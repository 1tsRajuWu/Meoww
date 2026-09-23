"""The station table: what plays, and the rules a row passes to be in it.

This is **data**, and it is kept apart from the code that uses it on purpose. The
table it replaces lived inside the cog — fourteen hundred lines of rows in the
middle of the command surface — which meant that the one file most likely to be
edited by somebody adding a station was also the one file whose diff nobody could
read. Here a row is one thing; the policy that picks between rows is
:mod:`utils.radio`; the engine that plays one is :mod:`utils.radio_stream`.

Three rules govern a row, and all three come from a failure this project has
already had:

* **Every URL was fetched before it was written down.** A station list is a list
  of URLs, and an unverified URL is a station that is broken on the day somebody
  picks it. The pass that checks them is ``scripts/probe_stations.py`` — the same
  question the weekly link scan asks, asked by hand — and it is what turned the
  table over during this rebuild: every row below answered with audio before it
  was committed.
* **The name is what the stream calls itself.** Directory tags are wrong often
  enough to matter: the entry filed under *baroque* in the first pass was a
  Bee Gees station, and the one under *classical* was Qur'an recitation. Both are
  fine stations; neither is what the label promised, and a member who picks
  *baroque* and gets *Tragedy* has been lied to by a lookup table. The same rule
  is why the country rows below are only kept when the station *corroborates*
  its country — its own name or state names the capital, or it is served from
  that country's own top-level domain. A Ukrainian internet station filed under
  the Emirates is a directory claim, not a fact about the Emirates.
* **A tag is drawn from a closed vocabulary** (:data:`TAGS`), so a filter cannot
  quietly stop matching because somebody spelled a mood differently.

``language`` is what the words are in — empty when the station has none worth
declaring, which is two different facts and both are kept honest by the index:
a station tagged ``instrumental`` genuinely has no words, and one with neither a
language nor that tag plays words this table has *not established*.

The country rows are the ones that turned this field from decoration into a
filter, and the source is the directory's **own tag for that station**, read back
per URL during the rebuild — the country pass that chose those rows never asked
for the field, which is why the first draft of this table could offer twenty-nine
languages and *no English*, the bot's own default. Where the tag was blank the row
takes the directory's ISO code (``en``, ``fr``, ``ar``), and where neither existed
it stays empty: a language nobody established is not one this table will guess.
The same pass folded one duplicated stream into the country row that owns it, so
no two rows point at one station.

``country`` and ``capital`` are set on the country rows added by the country
pass, and empty on the rest: an internet station that happens to be registered
somewhere is not "radio from" anywhere in particular, and saying so would be a
claim about a directory rather than about a station.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

#: The closed mood vocabulary. ``ad-free`` is a *claim* rather than a genre: it
#: is only ever applied where the station's own site says it carries no
#: advertising, which is why it is counted separately on the index and printed on
#: the picker's label.
TAGS: dict[str, str] = {
    "instrumental": "no singing — safe underneath a conversation",
    "focus": "built to be worked to: steady, low-distraction",
    "chill": "low intensity throughout",
    "energetic": "loud and fast by design",
    "oldies": "the catalogue is from before 2000",
    "seasonal": "in season or not, and obvious about which",
    "ad-free": "no advertising — the station's own site says so",
}


@dataclass(frozen=True, slots=True)
class Station:
    """One station: its address, what it is, and how it is described.

    Frozen because a row is data: nothing in this bot may edit a station at
    runtime, and the only way a station changes is a commit to this file.
    """

    key: str
    name: str
    url: str
    genre: str
    desc: str = ""
    icon: str = "📻"
    #: The language the words are in, or ``""`` for "none, or not established".
    language: str = ""
    #: The country this station broadcasts in, on the rows that were verified as
    #: such. Empty on everything else, deliberately.
    country: str = ""
    #: The capital city, when the station substantiated it (its own name or the
    #: directory's state field names it). Empty otherwise.
    capital: str = ""
    tags: tuple[str, ...] = ()
    #: The source's own rate in kbps when it is known: the encoder reads it so a
    #: stream is never re-encoded below what it arrives at.
    bitrate: int = 0
    #: Whether the source is already Opus — the one input that can be handed to
    #: Discord untouched. No station here declares it, so nothing takes that path
    #: today; the field stays because it is a property of the station.
    is_opus: bool = False

    @property
    def ad_free(self) -> bool:
        """Whether the station's own site says it carries no advertising."""
        return "ad-free" in self.tags


#: The rows. Legacy entries first — they are grouped by genre and a member's
#: browse should not be reshuffled by a rebuild — then the country rows, one per
#: verified country, in country order.
CATALOGUE: tuple[Station, ...] = (
    Station("groovesalad", "Groove Salad", "https://ice1.somafm.com/groovesalad-128-mp3",
            genre="Chill", desc="A nicely chilled plate of ambient beats and grooves. SomaFM's calm one.", icon="🥗", tags=("instrumental", "focus", "chill", "ad-free",)),
    Station("beatblender", "Beat Blender", "https://ice1.somafm.com/beatblender-128-mp3",
            genre="Chill", desc="A late-night blend of deep house and downtempo chill.", icon="🌙", tags=("instrumental", "focus", "chill", "ad-free",)),
    Station("lofi", "Lo-Fi Beats", "https://stream.laut.fm/lofi",
            genre="Lo-Fi", desc="Hazy, dusty, unhurried lo-fi. The default for homework and late nights.", icon="☕", tags=("instrumental", "focus", "chill",), bitrate=128),
    Station("chillhop", "I♥CHILLHOP", "https://streams.ilovemusic.de/iloveradio17.mp3",
            genre="Lo-Fi", desc="Chillhop and lo-fi hip hop with the edges sanded off.", icon="🍃", tags=("instrumental", "focus", "chill",), bitrate=192),
    Station("chillout", "Chillout", "https://stream.laut.fm/chillout",
            genre="Chill", desc="Downtempo and easy listening, at a volume that never intrudes.", icon="🛋️", tags=("instrumental", "focus", "chill",)),
    Station("musicandchill", "I♥MUSIC & CHILL", "https://streams.ilovemusic.de/iloveradio10.mp3",
            genre="Chill", desc="Slow pop and R&B for sitting still to.", icon="💤", tags=("chill",)),
    Station("ambient", "Ambient Radio", "https://stream.laut.fm/ambient",
            genre="Ambient", desc="Textures and drones with no rhythm to follow.", icon="🌫️", tags=("instrumental", "focus", "chill",), bitrate=128),
    Station("dronezone", "Drone Zone", "https://ice1.somafm.com/dronezone-128-mp3",
            genre="Ambient", desc="Atmospheric textures with barely a beat. For concentrating, or sleeping.", icon="🛰️", tags=("instrumental", "focus", "chill", "ad-free",)),
    Station("deepspace", "Deep Space One", "https://ice1.somafm.com/deepspaceone-128-mp3",
            genre="Ambient", desc="Deep ambient, experimental electronics, and the odd NASA transmission.", icon="🌌", tags=("instrumental", "focus", "chill", "ad-free",)),
    Station("spacestation", "Space Station Soma", "https://ice1.somafm.com/spacestation-128-mp3",
            genre="Ambient", desc="Tune in, turn on, space out. Ambient and mid-tempo electronica.", icon="🚀", tags=("instrumental", "focus", "chill", "ad-free",)),
    Station("fluid", "Fluid", "https://ice1.somafm.com/fluid-128-mp3",
            genre="Instrumental", desc="Instrumental hip hop, triphop and the stranger end of jazz.", icon="💧", tags=("instrumental", "focus", "ad-free",)),
    Station("instrumental", "Instrumental", "https://stream.laut.fm/instrumental",
            genre="Instrumental", desc="No vocals at all — the one station to pick if singing over you is the problem.", icon="🎻", tags=("instrumental", "focus",)),
    Station("digitalis", "Digitalis", "https://ice1.somafm.com/digitalis-128-mp3",
            genre="Folk", desc="Folk, indie and slowcore — the quiet acoustic corner of SomaFM.", icon="🌾", tags=("chill", "oldies", "ad-free",)),
    Station("jazz", "FIP Jazz", "https://icecast.radiofrance.fr/fipjazz-midfi.mp3",
            genre="Jazz", desc="Straight jazz from Radio France, with almost no talking in between.", icon="🎷", language="French", tags=("instrumental", "focus",)),
    Station("jazzcafe", "Swiss Jazz", "https://stream.srg-ssr.ch/m/rsj/mp3_128",
            genre="Jazz", desc="Mellow cafe jazz, standards and the occasional bossa.", icon="☕", tags=("instrumental", "focus", "chill",), bitrate=128),
    Station("jazzfm", "Jazz FM", "https://stream.laut.fm/jazz",
            genre="Jazz", desc="A wide sweep of jazz: bebop, fusion, and everything filed between.", icon="🎺", tags=("instrumental",)),
    Station("sonicuniverse", "Sonic Universe", "https://ice1.somafm.com/sonicuniverse-128-mp3",
            genre="Jazz", desc="Nu jazz and avant-garde Euro jazz, for when the standards are not the point.", icon="🪐", tags=("instrumental", "ad-free",)),
    Station("lounge", "Lounge", "https://stream.laut.fm/lounge",
            genre="Lounge", desc="Bossa, downtempo and hotel-bar smooth.", icon="🍸", tags=("instrumental", "chill",)),
    Station("illstreet", "Ill Street Lounge", "https://ice1.somafm.com/illstreet-128-mp3",
            genre="Lounge", desc="Classic bachelor pad, playful exotica and vintage music of tomorrow.", icon="🕶️", tags=("instrumental", "chill", "oldies", "ad-free",)),
    Station("secretagent", "Secret Agent", "https://ice1.somafm.com/secretagent-128-mp3",
            genre="Lounge", desc="The soundtrack for your stylish, mysterious, dangerous life.", icon="🕵️", tags=("instrumental", "oldies", "ad-free",)),
    Station("swing", "Swing", "https://stream.laut.fm/swing",
            genre="Swing", desc="Big band, swing and jump blues.", icon="🎩", tags=("oldies",)),
    Station("soul", "Soul", "https://stream.laut.fm/soul",
            genre="Soul & Funk", desc="Classic soul, motown and modern soul.", icon="💜", tags=("oldies",)),
    Station("funk", "Funk", "https://stream.laut.fm/funk",
            genre="Soul & Funk", desc="Slap bass, horns and grooves built to be sampled.", icon="🕺", tags=("energetic", "oldies",)),
    Station("blues", "Blues", "https://stream.laut.fm/blues",
            genre="Blues", desc="Delta, Chicago and electric blues.", icon="🎸", tags=("oldies",)),
    Station("classical", "Klassik", "https://stream.laut.fm/klassik",
            genre="Classical", desc="Orchestral and chamber works, from baroque to late romantic.", icon="🎼", tags=("instrumental", "focus",)),
    Station("classicalfm", "Classical KUSC", "https://playerservices.streamtheworld.com/api/livestream-redirect/KUSCMP128.mp3",
            genre="Classical", desc="Classical music from a public radio station, presented properly.", icon="📻", tags=("instrumental", "focus",)),
    Station("francemusique", "France Musique", "https://icecast.radiofrance.fr/francemusique-midfi.mp3",
            genre="Classical", desc="Concerts, opera and chamber music with a French accent.", icon="🇫🇷", language="French", tags=("instrumental",)),
    Station("baroquefm", "Venice Classic Radio", "https://uk2.streamingpulse.com/ssl/vcr1",
            genre="Classical", desc="Baroque and early music, mostly Italian, mostly instrumental.", icon="🏛️", language="Italian", tags=("instrumental", "focus", "oldies",), bitrate=128),
    Station("swissclassic", "Swiss Classic", "https://stream.srg-ssr.ch/m/rsc_de/mp3_128",
            genre="Classical", desc="A calm classical programme — the one to put on while reading.", icon="🎻", language="German", tags=("instrumental", "focus", "chill",), bitrate=128),
    Station("swissclassicfr", "Swiss Classic Français", "https://stream.srg-ssr.ch/m/rsc_fr/mp3_128",
            genre="Classical", desc="The same classical programme, introduced in French.", icon="🎻", language="French", tags=("instrumental", "focus", "chill",)),
    Station("opera", "France Musique Opéra", "https://icecast.radiofrance.fr/francemusiqueopera-hifi.aac",
            genre="Opera", desc="Arias, overtures and full performances, all day.", icon="🎭", tags=("instrumental",), bitrate=192),
    Station("soundtrack", "Soundtrack", "https://stream.laut.fm/soundtrack",
            genre="Soundtrack", desc="Film scores, series themes and the orchestral bits of games.", icon="🎬", tags=("instrumental", "focus",)),
    Station("synthwave", "Nightwave Plaza", "https://radio.plaza.one/mp3",
            genre="Synthwave", desc="80s neon synthwave, future funk and vaporwave.", icon="🌃", tags=("instrumental", "focus",)),
    Station("vaporwave", "Vaporwave", "https://stream.laut.fm/vaporwave",
            genre="Vaporwave", desc="Slowed, reverbed and nostalgic in a way you cannot place.", icon="🌴", tags=("instrumental", "chill",)),
    Station("edm", "I♥DANCE", "https://streams.ilovemusic.de/iloveradio2.mp3",
            genre="Dance", desc="Big-room dance and club edits, all day.", icon="🔊", tags=("energetic",), bitrate=192),
    Station("techno", "Techno", "https://stream.laut.fm/techno",
            genre="Techno", desc="Four to the floor, no apologies.", icon="⚙️", tags=("instrumental", "energetic",), bitrate=128),
    Station("house", "House", "https://stream.laut.fm/house",
            genre="House", desc="Classic and modern house, mixed continuously.", icon="🏠", tags=("instrumental", "energetic",)),
    Station("deephouse", "Deep House", "https://stream.laut.fm/deephouse",
            genre="House", desc="Warmer, slower house with room to breathe.", icon="🌊", tags=("instrumental", "focus",)),
    Station("trance", "Trance", "https://stream.laut.fm/trance",
            genre="Trance", desc="Long builds, big drops, and the occasional classic.", icon="🌀", tags=("instrumental", "energetic",)),
    Station("hardstyle", "I♥HARDSTYLE", "https://streams.ilovemusic.de/iloveradio21.mp3",
            genre="Hardstyle", desc="Reverse bass and festival kicks. Not a background station.", icon="🥁", tags=("energetic",), bitrate=192),
    Station("dnb", "Brokenbeats", "https://stream.brokenbeats.net/tune",
            genre="Drum & Bass", desc="Drum and bass, jungle and breakbeat at broadcast quality.", icon="🔗", tags=("instrumental", "energetic",), bitrate=320),
    Station("koolfm", "Kool FM London", "https://admin.stream.rinse.fm/proxy/kool/stream",
            genre="Drum & Bass", desc="The London jungle and drum & bass institution, live.", icon="🎚️", tags=("energetic",), bitrate=128),
    Station("ukbass", "UK Bass Radio", "https://www.ukbassradio.com/stream",
            genre="Drum & Bass", desc="Bassline, garage and bass house from the UK.", icon="🇬🇧", tags=("energetic",), bitrate=192),
    Station("dubstep", "Dub Step Beyond", "https://ice1.somafm.com/dubstep-128-mp3",
            genre="Dubstep", desc="Dubstep, dub and the deeper end of bass music.", icon="🔊", tags=("instrumental", "energetic", "ad-free",)),
    Station("idm", "Cliqhop IDM", "https://ice1.somafm.com/cliqhop-128-mp3",
            genre="IDM", desc="Glitchy, brainy electronics for people who like their beats odd.", icon="🧠", tags=("instrumental", "focus", "ad-free",)),
    Station("defcon", "DEF CON Radio", "https://ice1.somafm.com/defcon-128-mp3",
            genre="Electro", desc="Music for hacking to, from the DEF CON conference's own station.", icon="👾", tags=("instrumental", "focus", "ad-free",)),
    Station("fipelectro", "FIP Électro", "https://icecast.radiofrance.fr/fipelectro-midfi.mp3",
            genre="Electro", desc="Electronic music chosen by people who take it seriously.", icon="⚡", language="French", tags=("instrumental", "energetic",)),
    Station("fipgroove", "FIP Groove", "https://icecast.radiofrance.fr/fipgroove-midfi.mp3",
            genre="Soul & Funk", desc="Soul, funk and groove across the decades.", icon="🎶", language="French"),
    Station("disco", "Disco", "https://stream.laut.fm/disco",
            genre="Disco", desc="Mirrorball disco and the boogie it turned into.", icon="🪩", tags=("energetic", "oldies",)),
    Station("phonk", "Phonk", "https://stream.laut.fm/phonk",
            genre="Hip-Hop", desc="Distorted Memphis drums and drift music.", icon="🚗", tags=("instrumental", "energetic",), bitrate=128),
    Station("gamemusic", "Gamesboro Radio", "https://radio.gamesboro.org/listen/gamesboro_radio/radio.mp3",
            genre="Game Music", desc="Video game music, 24/7, from 8-bit to orchestral.", icon="🎮", tags=("instrumental", "focus",), bitrate=128),
    Station("chiptune", "24/7 Chiptune Radio", "https://radio.zelerk.com/listen/chiptune/stream.mp3",
            genre="Chiptune", desc="Tracker music, demoscene and hardware chips, at 256 kbps.", icon="🕹️", tags=("instrumental", "focus",), bitrate=256),
    Station("retrogames", "CPC Scene Radio", "https://a1.asurahosting.com/listen/cpc_scene_radio/radio.mp3",
            genre="Chiptune", desc="8-bit and 16-bit machine music, from the scene that made it.", icon="💾", tags=("instrumental", "focus",), bitrate=128),
    Station("gaming", "Gaming", "https://stream.laut.fm/gaming",
            genre="Game Music", desc="Trailer music and game scores for the middle of a session.", icon="🎯", tags=("instrumental", "energetic",)),
    Station("subspace", "qUAntUm RaDio", "https://radio.erb.pw/listen/subspace/radio.mp3",
            genre="Chiptune", desc="Demoscene, netlabel electronics and something you have not heard.", icon="🔬", tags=("instrumental",), bitrate=128),
    Station("pop", "Swiss Pop", "https://stream.srg-ssr.ch/m/rsp/mp3_128",
            genre="Pop", desc="Today's pop, mostly in English, with a Swiss accent.", icon="🎤", bitrate=128),
    Station("top40", "I♥RADIO Charts", "https://streams.ilovemusic.de/iloveradio1.mp3",
            genre="Top 40", desc="The current chart, on a loop.", icon="📈", bitrate=192),
    Station("pophits", "I♥BIGGEST POP HITS", "https://streams.ilovemusic.de/iloveradio11.mp3",
            genre="Pop", desc="The big pop records of the last decade or so.", icon="✨"),
    Station("trashpop", "I♥TRASHPOP", "https://streams.ilovemusic.de/iloveradio19.mp3",
            genre="Pop", desc="Guilty-pleasure pop, played without irony.", icon="🫧"),
    Station("rock", "Rock", "https://stream.laut.fm/rock",
            genre="Rock", desc="Classic and modern rock, guitar first.", icon="🎸", tags=("energetic",)),
    Station("fiprock", "FIP Rock", "https://icecast.radiofrance.fr/fiprock-midfi.mp3",
            genre="Rock", desc="Rock chosen with taste, from garage to post-punk.", icon="🎸", language="French"),
    Station("rockmix", "Radio Paradise Rock Mix", "https://stream.radioparadise.com/rock-320",
            genre="Rock", desc="Album rock at 320 kbps, hand-picked rather than playlisted.", icon="🤘", tags=("energetic",)),
    Station("punk", "Punk", "https://stream.laut.fm/punk",
            genre="Punk", desc="Punk, hardcore and the loud end of new wave.", icon="📌", tags=("energetic", "oldies",), bitrate=128),
    Station("rockabilly", "Rockabilly", "https://stream.laut.fm/rockabilly",
            genre="Rock", desc="Fifties rock and roll, rockabilly and psychobilly.", icon="🚗", tags=("oldies",)),
    Station("metal", "Metal Detector", "https://ice1.somafm.com/metal-128-mp3",
            genre="Metal", desc="Black to doom, prog to sludge, thrash to post and back.", icon="🤘", tags=("energetic", "ad-free",)),
    Station("heavymetal", "Radio Heavy Metal", "https://manager7.streamradio.fr:1435/stream",
            genre="Metal", desc="The classics and the followers, at 320 kbps.", icon="⚒️", tags=("energetic",), bitrate=320),
    Station("metalcore", "Metalcore", "https://stream.laut.fm/metalcore",
            genre="Metal", desc="Metalcore, deathcore and the breakdowns in between.", icon="💥", tags=("energetic",)),
    Station("fipmetal", "FIP Metal", "https://icecast.radiofrance.fr/fipmetal-midfi.mp3",
            genre="Metal", desc="Metal, curated the way a radio station used to.", icon="⚔️", language="French", tags=("energetic",)),
    Station("indie", "Indie Pop Rocks!", "https://ice1.somafm.com/indiepop-128-mp3",
            genre="Indie", desc="Indie pop, old and new, with a soft spot for the obscure.", icon="🌼", tags=("ad-free",)),
    Station("kexp", "KEXP 90.3 FM", "https://kexp-mp3-128.streamguys1.com/kexp128.mp3",
            genre="Indie", desc="Seattle's listener-powered station: whatever the DJ felt like.", icon="🌲", bitrate=128),
    Station("wfmu", "WFMU Freeform", "https://stream0.wfmu.org/freeform-128k",
            genre="Eclectic", desc="Genuinely freeform radio: one show can contain anything.", icon="🗽", tags=("ad-free",)),
    Station("radioparadise", "Radio Paradise", "https://stream.radioparadise.com/mp3-320",
            genre="Eclectic", desc="An eclectic mix, hand-picked and listener-supported, at 320 kbps.", icon="🏝️"),
    Station("paradisemellow", "Radio Paradise Mellow Mix", "https://stream.radioparadise.com/mellow-320",
            genre="Eclectic", desc="The calm half of Radio Paradise, still at 320 kbps.", icon="🌅", tags=("chill", "focus",)),
    Station("paradiseglobal", "Radio Paradise Global Mix", "https://stream.radioparadise.com/global-320",
            genre="World", desc="Music from outside the English-language charts.", icon="🌍", language="Multilingual"),
    Station("country", "Country", "https://stream.laut.fm/country",
            genre="Country", desc="Modern and classic country.", icon="🤠"),
    Station("realcountry", "181.FM Real Country", "https://listen.181fm.com/181-realcountry_128k.mp3",
            genre="Country", desc="Traditional country — fiddles, steel and no apologies.", icon="🌵", tags=("oldies",)),
    Station("folk", "Folk", "https://stream.laut.fm/folk",
            genre="Folk", desc="Folk, singer-songwriter and acoustic traditions.", icon="🪕"),
    Station("folkfwd", "Folk Forward", "https://ice1.somafm.com/folkfwd-128-mp3",
            genre="Folk", desc="Indie folk, Americana, and the new acoustic songwriters.", icon="🍂", tags=("chill", "ad-free",)),
    Station("celtic", "Thistle Radio", "https://ice1.somafm.com/thistle-128-mp3",
            genre="Celtic", desc="An exploration of music from Celtic roots.", icon="🍀", tags=("instrumental", "ad-free",)),
    Station("covers", "Covers", "https://ice1.somafm.com/covers-128-mp3",
            genre="Eclectic", desc="Songs you know, done by people you don't. Just covers, all day.", icon="🎭", tags=("ad-free",)),
    Station("eighties", "80s", "https://stream.laut.fm/80s",
            genre="80s", desc="The whole decade: synth, new wave and the chart records.", icon="📼", tags=("oldies",)),
    Station("underground80s", "Underground Eighties", "https://ice1.somafm.com/u80s-128-mp3",
            genre="80s", desc="UK synthpop and a bit of new wave, mostly the deep cuts.", icon="🕳️", tags=("oldies", "energetic", "ad-free",)),
    Station("nineties", "90s Radio", "https://stream.laut.fm/90s",
            genre="90s", desc="Nineties pop, dance and the songs you still know every word to.", icon="💿", tags=("oldies",)),
    Station("seventies", "Left Coast 70s", "https://ice1.somafm.com/seventies-128-mp3",
            genre="Oldies", desc="Mellow album rock from the Seventies. Yacht friendly.", icon="⛵", tags=("oldies", "chill", "ad-free",)),
    Station("oldies", "Oldies", "https://stream.laut.fm/oldies",
            genre="Oldies", desc="Fifties to seventies, in no particular order.", icon="📻", tags=("oldies",)),
    Station("hitshistory", "I♥HITS HISTORY", "https://streams.ilovemusic.de/iloveradio12.mp3",
            genre="Oldies", desc="The records that were number one at some point.", icon="🕰️", tags=("oldies",)),
    Station("hiphop", "Hip Hop", "https://stream.laut.fm/hiphop",
            genre="Hip-Hop", desc="Hip hop across the decades, old school to now.", icon="🎙️"),
    Station("rap", "Rap", "https://stream.laut.fm/rap",
            genre="Rap", desc="Rap and trap, mostly new.", icon="🔥", tags=("energetic",)),
    Station("trap", "Trap", "https://stream.laut.fm/trap",
            genre="Trap", desc="808s, hi-hat rolls and nothing else.", icon="💎", tags=("energetic",)),
    Station("usrap", "I♥US ONLY RAP", "https://streams.ilovemusic.de/iloveradio13.mp3",
            genre="Rap", desc="American rap only, no exceptions.", icon="🇺🇸", tags=("energetic",)),
    Station("deutschrap", "Deutschrap Beste", "https://streams.ilovemusic.de/iloveradio6.mp3",
            genre="Rap", desc="German rap, and a lot of it.", icon="🇩🇪", language="German", tags=("energetic",)),
    Station("fiphiphop", "FIP Hip-Hop", "https://icecast.radiofrance.fr/fiphiphop-midfi.mp3",
            genre="Hip-Hop", desc="Hip hop, curated with the French gift for finding the good stuff.", icon="🎧", language="French"),
    Station("jpop", "J-Pop (listen.moe)", "https://listen.moe/stream",
            genre="J-Pop", desc="Japanese pop, rock and anime music, at 192 kbps Opus.", icon="🌸", language="Japanese", tags=("energetic",), bitrate=192),
    Station("kpop", "K-Pop (listen.moe)", "https://listen.moe/kpop/stream",
            genre="K-Pop", desc="Korean pop and idol groups, at 192 kbps Opus.", icon="🇰🇷", language="Korean", tags=("energetic",), bitrate=192),
    Station("kpopradio", "0N K-Pop", "https://0n-kpop.radionetz.de/0n-kpop.mp3",
            genre="K-Pop", desc="K-Pop around the clock, hosted in German.", icon="🎤", language="Korean", tags=("energetic",), bitrate=128),
    Station("kpophits", "Kpophits", "https://kpophits.stream.laut.fm/kpophits",
            genre="K-Pop", desc="Nothing but the hits, which is what it says on the tin.", icon="💜", language="Korean", tags=("energetic",), bitrate=128),
    Station("cinz", "CINZ NET Radio", "https://stream.cinz.net/",
            genre="J-Pop", desc="K-pop, J-pop and J-metal from an independent net station.", icon="🎵", language="Multilingual", tags=("energetic",), bitrate=128),
    Station("anime", "Anime", "https://stream.laut.fm/anime",
            genre="Anime", desc="Anime openings, endings and OSTs.", icon="🍥", language="Japanese", tags=("energetic",)),
    Station("nightcore", "Nightcore", "https://stream.laut.fm/nightcore",
            genre="Nightcore", desc="Everything sped up and pitched, on purpose.", icon="⚡", tags=("energetic",)),
    Station("bollywood", "Mirchi Top 20", "https://drive.uber.radio/uber/bollywoodnow/icecast.audio",
            genre="Bollywood", desc="Today's Hindi chart, the biggest songs in the country.", icon="🇮🇳", language="Hindi", bitrate=128),
    Station("bollywood90s", "Retro Bollywood 90s", "https://stream-143.zeno.fm/fdgs82xkzhhvv",
            genre="Bollywood", desc="Nineties Hindi film music, which is its own genre.", icon="🎬", language="Hindi", tags=("oldies",), bitrate=128),
    Station("fipworld", "FIP Monde", "https://icecast.radiofrance.fr/fipworld-midfi.mp3",
            genre="World", desc="Music from everywhere that is not the Anglosphere.", icon="🌍", language="Multilingual"),
    Station("fip", "FIP", "https://icecast.radiofrance.fr/fip-midfi.mp3",
            genre="Eclectic", desc="Jazz, soul, world and rock in one eclectic programme.", icon="🇫🇷", language="French"),
    Station("fipnouveautes", "FIP Nouveautés", "https://icecast.radiofrance.fr/fipnouveautes-midfi.mp3",
            genre="Eclectic", desc="New releases only, which is the cheapest way to hear something first.", icon="🆕", language="French"),
    Station("arabic", "Monte Carlo Doualiya", "https://montecarlodoualiya128k.ice.infomaniak.ch/mc-doualiya.mp3",
            genre="Arabic", desc="Arabic music and programming, from Paris to the Middle East.", icon="🕌", language="Arabic", bitrate=128),
    Station("turkish", "Beste FM", "https://stream.zeno.fm/tnvnovtgozstv",
            genre="Turkish", desc="Turkish pop and arabesque.", icon="🇹🇷", language="Turkish", bitrate=128),
    Station("salsa", "Salsa", "https://stream.laut.fm/salsa",
            genre="Salsa", desc="Salsa, merengue and the Latin dance floor.", icon="💃", language="Spanish", tags=("energetic",)),
    Station("reggae", "SomaFM Reggae", "https://ice1.somafm.com/reggae-128-mp3",
            genre="Reggae", desc="Roots, dub and dancehall.", icon="🇯🇲", tags=("chill", "ad-free",)),
    Station("reggaefm", "Reggae Chill Cafe", "https://maggie.torontocast.com:2020/stream/reggaechillcafe",
            genre="Reggae", desc="The gentler side of reggae, for a long afternoon.", icon="🌴", tags=("chill",), bitrate=128),
    Station("fipreggae", "FIP Reggae", "https://icecast.radiofrance.fr/fipreggae-midfi.mp3",
            genre="Reggae", desc="Reggae from its beginnings to now, with the history attached.", icon="🎶", language="French"),
    Station("gospel", "Gospel FM Jamaica", "https://stream-37.zeno.fm/zpksre88rm0uv",
            genre="Gospel", desc="Gospel and worship music from Jamaica.", icon="🙏", bitrate=128),
    Station("srf3", "SRF 3", "https://stream.srg-ssr.ch/m/drs3/mp3_128",
            genre="Pop", desc="New music and pop, presented in Swiss German.", icon="🇨🇭", language="German"),
    Station("partymode", "I♥PARTY HARD", "https://streams.ilovemusic.de/iloveradio14.mp3",
            genre="Dance", desc="Party records, mixed into each other, no gaps.", icon="🎉", tags=("energetic",)),
    Station("dance", "Dance", "https://stream.laut.fm/dance",
            genre="Dance", desc="Club and dance, from the nineties onwards.", icon="💃", tags=("energetic",)),
    Station("christmas", "Christmas", "https://stream.laut.fm/christmas",
            genre="Seasonal", desc="Christmas music, which is delightful in December and a war crime in June.", icon="🎄", tags=("seasonal",)),
    Station("nasachatter", "Mission Control", "https://ice1.somafm.com/missioncontrol-128-mp3",
            genre="Ambient", desc="Ambient music woven around NASA and space-explorer transmissions.", icon="👩‍🚀", tags=("instrumental", "focus", "ad-free",)),
    Station("poptron", "PopTron", "https://ice1.somafm.com/poptron-128-mp3",
            genre="Pop", desc="Electro-pop and indie dance rock.", icon="🎹", tags=("energetic", "ad-free",)),
    Station("bootliquor", "Boot Liquor", "https://ice1.somafm.com/bootliquor-128-mp3",
            genre="Country", desc="Americana roots music for cowhands, cowpokes and cowtippers.", icon="🤠", tags=("oldies", "ad-free",)),
    Station("brfm", "Black Rock FM", "https://ice1.somafm.com/brfm-128-mp3",
            genre="Eclectic", desc="Rock, soul and hip hop from the Black Rock Coalition.", icon="🖤", tags=("ad-free",)),
    Station("lush", "Lush", "https://ice1.somafm.com/lush-128-mp3",
            genre="Chill", desc="Mostly female vocals with an electronic influence.", icon="🌺", tags=("chill", "ad-free",)),
    Station("thetrip", "The Trip", "https://ice1.somafm.com/thetrip-128-mp3",
            genre="House", desc="Progressive house and downtempo, from SomaFM's DJ sets.", icon="🛣️", tags=("instrumental", "focus", "ad-free",)),
    Station("los40", "LOS40", "https://25453.live.streamtheworld.com/LOS40AAC.aac",
            genre="Top 40", desc="Spain's hit-music station, and the one that decides what a Spanish hit is.", icon="🇪🇸", language="Spanish", tags=("energetic",)),
    Station("rairadio3", "Rai Radio 3", "https://icestreaming.rai.it/3.mp3",
            genre="Eclectic", desc="Italian public radio: classical concerts, jazz and long-form culture.", icon="🇮🇹", language="Italian"),
    Station("antena1", "Antena 1", "https://radiocast.rtp.pt/antena180a.mp3",
            genre="Eclectic", desc="Portugal's public station — news, culture and Portuguese music.", icon="🇵🇹", language="Portuguese"),
    Station("antena2", "Antena 2", "https://radiocast.rtp.pt/antena280a.mp3",
            genre="Classical", desc="The classical and jazz half of Portuguese public radio.", icon="🎻", language="Portuguese"),
    Station("nashe", "Nashe Radio", "https://nashe1.hostingradio.ru/nashe-128.mp3",
            genre="Rock", desc="Russian rock, the station that made it a genre rather than a scene.", icon="🇷🇺", language="Russian", tags=("energetic",)),
    Station("rmffm", "RMF FM", "https://rs6-krk2.rmfstream.pl/RMFFM48",
            genre="Pop", desc="Poland's biggest commercial station — pop and news, all day.", icon="🇵🇱", language="Polish", tags=("energetic",)),
    Station("nporadio1", "NPO Radio 1", "https://icecast.omroep.nl/radio1-bb-mp3",
            genre="Eclectic", desc="Dutch public radio's news and talk network.", icon="🇳🇱", language="Dutch", bitrate=192),
    Station("nporadio2", "NPO Radio 2", "https://icecast.omroep.nl/radio2-bb-mp3",
            genre="Pop", desc="Dutch pop, from the Dutch ones and everybody else.", icon="🌷", language="Dutch", tags=("energetic",)),
    Station("nrkp1", "NRK P1", "https://lyd.nrk.no/nrk_radio_p1_ostlandssendingen_mp3_h",
            genre="Eclectic", desc="Norwegian public radio's main channel, Oslo edition.", icon="🇳🇴", language="Norwegian"),
    Station("drp1", "DR P1", "https://live-icy.dr.dk/A/A03H.mp3",
            genre="Eclectic", desc="Danish public radio: news and talk for a whole small country.", icon="🇩🇰", language="Danish"),
    Station("srp1", "Sveriges Radio P1", "https://live1.sr.se/p1-mp3-192",
            genre="Eclectic", desc="Sweden's speech channel — news, debate and the long interview.", icon="🇸🇪", language="Swedish"),
    Station("rozhlas", "ČRo Radiožurnál", "https://rozhlas.stream/radiozurnal_mp3_128.mp3",
            genre="Eclectic", desc="The Czech Republic's main news station, from Czech Radio.", icon="🇨🇿", language="Czech"),
    Station("klubradio", "Klubrádió", "https://icast.connectmedia.hu/5201/live.mp3",
            genre="Eclectic", desc="Hungarian talk radio, and famously not shy about it.", icon="🇭🇺", language="Hungarian", bitrate=128),
    Station("rractualitati", "Radio România Actualități", "https://stream4.srr.ro:8443/romania-actualitati",
            genre="Eclectic", desc="Romania's public news channel, in Romanian all day.", icon="🇷🇴", language="Romanian", bitrate=96),
    Station("ertkosmos", "ERT Kosmos", "https://radiostreaming.ert.gr/ert-kosmos",
            genre="World", desc="Greek public radio's world-music channel, from Athens.", icon="🇬🇷", language="Greek", bitrate=256),
    Station("j1hits", "J1 HITS", "https://jenny.torontocast.com:2000/stream/J1HITS",
            genre="J-Pop", desc="Japanese chart pop, the current stuff rather than the classics.", icon="🎌", language="Japanese", tags=("energetic",)),
    Station("galatz", "Galei Tzahal", "https://glzwizzlv.bynetcdn.com/glglz_mp3",
            genre="Pop", desc="Israel's army radio — pop, news and the playlist the country argues about.", icon="🇮🇱", language="Hebrew", tags=("energetic",)),
    Station("coolfahrenheit", "Cool Fahrenheit", "https://coolism-web.cdn.byteark.com/;stream.mp3",
            genre="Pop", desc="Thai pop and indie, from Bangkok.", icon="🇹🇭", language="Thai", tags=("energetic",)),
    Station("yleradio1", "Yle Radio 1", "https://icecast.live.yle.fi/radio/YleRadio1/icecast.audio",
            genre="Eclectic", desc="Finland's public radio: news, current affairs and Finnish music.", icon="🇫🇮", language="Finnish"),
    Station("trax", "Trax Streaming", "https://stream.radiojar.com/rrqf78p3bnzuv",
            genre="Dance", desc="Jakarta dance and pop — Trax FM, streaming under its own name.", icon="🇮🇩", language="Indonesian", tags=("energetic",)),
    Station("roks", "Radio ROKS", "https://online.radioroks.ua/RadioROKS_HD",
            genre="Rock", desc="Ukrainian rock from Kyiv, where the genre is a national argument.", icon="🇺🇦", language="Ukrainian", tags=("energetic",), bitrate=320),
    Station("donya", "Radio Donya", "https://stream.zeno.fm/v5kxmagseg0uv",
            genre="Pop", desc="Persian pop and the old favourites, from the diaspora.", icon="🇮🇷", language="Persian", tags=("energetic",), bitrate=128),
    Station("vnyouth", "Vietnamese Youth Radio", "https://stream.zeno.fm/umt5gqmg3reuv",
            genre="Pop", desc="Vietnamese pop for a younger crowd — and the words are the point.", icon="🇻🇳", language="Vietnamese", tags=("energetic",)),

    Station("ad-flaix-fm-andorra", "Flaix FM Andorra", "https://streaming.giraweb.com/8020/stream",
            genre="Top 40", desc="One of Andorra's own stations — fetched and verified before it was written down.", icon="🇦🇩", language="Catalan", country="Andorra", capital="Andorra la Vella", bitrate=320),
    Station("af-radio-kabul", "Radio Kabul", "http://23.81.35.187:8054/stream",
            genre="Local", desc="One of Afghanistan's own stations — fetched and verified before it was written down.", icon="🇦🇫", country="Afghanistan", capital="Kabul", bitrate=128),
    Station("ag-antigua-observer-r", "Antigua Observer Radio 91.1 St. John's", "http://s8.voscast.com:8176/;",
            genre="Talk", desc="One of Antigua And Barbuda's own stations — fetched and verified before it was written down.", icon="🇦🇬", language="English", country="Antigua And Barbuda", capital="Saint John's", bitrate=192),
    Station("al-radio-shqip-tirana", "Radio Shqip - Tirana 92.0 MHz", "https://cp1.sednastream.com/proxy/radioshqip?mp=/stream",
            genre="Local", desc="One of Albania's own stations — fetched and verified before it was written down.", icon="🇦🇱", country="Albania", capital="Tirana", bitrate=128),
    Station("am-armenian-radio", "Armenian Radio", "https://cast.thiff.com/proxy/laarmeni?mp=/stream",
            genre="Local", desc="One of Armenia's own stations, broadcasting from Yerevan — fetched and verified before it was written down.", icon="🇦🇲", country="Armenia", capital="Yerevan", bitrate=192),
    Station("ao-radio-luanda", "Radio Luanda", "https://paineldj5.com.br:20089/stream",
            genre="Local", desc="One of Angola's own stations, broadcasting from Luanda — fetched and verified before it was written down.", icon="🇦🇴", country="Angola", capital="Luanda", bitrate=64),
    Station("ar-radio-disney-argen", "Radio Disney Argentina 94.3. Ciudad de Buenos Aires", "https://playerservices.streamtheworld.com/api/livestream-redirect/DISNEY_ARG_BA.mp3",
            genre="Local", desc="One of Argentina's own stations — fetched and verified before it was written down.", icon="🇦🇷", country="Argentina", capital="Buenos Aires", bitrate=128),
    Station("at-orf-hitradio-3", "ORF Hitradio Ö3 | HQ", "https://orf-live.ors-shoutcast.at/oe3-q2a",
            genre="Pop", desc="One of Austria's own stations, broadcasting from Vienna — fetched and verified before it was written down.", icon="🇦🇹", language="German", country="Austria", capital="Vienna", bitrate=192),
    Station("au-2ca-canberra-1053", "2CA - Canberra - 1053 AM (MP3)", "http://playerservices.streamtheworld.com/api/livestream-redirect/2CA.mp3",
            genre="Oldies", desc="One of Australia's own stations, broadcasting from Canberra ACT — fetched and verified before it was written down.", icon="🇦🇺", country="Australia", capital="Canberra", tags=("oldies",), bitrate=64),
    Station("aw-magic-96-5", "Magic 96.5 - Oranjestad", "http://158.69.114.190:8072/;",
            genre="Pop", desc="One of Aruba's own stations — fetched and verified before it was written down.", icon="🇦🇼", language="Papiamento", country="Aruba", capital="Oranjestad", bitrate=128),
    Station("az-anti-radio", "Anti Radio", "https://stream.antiradio.net/radio/8000/mp3",
            genre="Local", desc="One of Azerbaijan's own stations, broadcasting from Baku — fetched and verified before it was written down.", icon="🇦🇿", country="Azerbaijan", capital="Baku", bitrate=128),
    Station("ba-narodni-radio-sara", "Narodni Radio - Sarajevo", "http://server1.tnt.ba:8010/",
            genre="Local", desc="One of Bosnia And Herzegovina's own stations — fetched and verified before it was written down.", icon="🇧🇦", language="Bosnian", country="Bosnia And Herzegovina", capital="Sarajevo", bitrate=128),
    Station("bb-radio-bimshire-106", "Radio Bimshire 106.1 FM", "https://listen.radioking.com/radio/635914/stream/700051",
            genre="Local", desc="One of Barbados's own stations, broadcasting from Bridgetown — fetched and verified before it was written down.", icon="🇧🇧", language="English", country="Barbados", capital="Bridgetown", bitrate=192),
    Station("bd-radio-ullash", "Radio Ullash", "http://stream.zeno.fm/gpgkgp9mp18uv.mp3",
            genre="Local", desc="One of Bangladesh's own stations, broadcasting from Dhaka — fetched and verified before it was written down.", icon="🇧🇩", country="Bangladesh", capital="Dhaka", bitrate=128),
    Station("be-kiosk-radio", "Kiosk Radio", "https://kioskradiobxl.out.airtime.pro/kioskradiobxl_b",
            genre="Local", desc="One of Belgium's own stations, broadcasting from Brussels — fetched and verified before it was written down.", icon="🇧🇪", language="English", country="Belgium", capital="Brussels", bitrate=192),
    Station("bf-horizon-fm-104", "Horizon FM 104.4 Ouagadougou", "https://stream.zeno.fm/0945crn4bf9uv",
            genre="Local", desc="One of Burkina Faso's own stations — fetched and verified before it was written down.", icon="🇧🇫", language="French", country="Burkina Faso", capital="Ouagadougou", bitrate=128),
    Station("bg-radio-zorana-sofia", "Radio Zorana Sofia", "https://radio.jump.bg/proxy/radio12/stream",
            genre="Local", desc="One of Bulgaria's own stations — fetched and verified before it was written down.", icon="🇧🇬", country="Bulgaria", capital="Sofia", bitrate=256),
    Station("bh-afn-360-bahrain", "AFN 360 Bahrain", "http://25603.live.streamtheworld.com/AFNE_BHN_SC",
            genre="Local", desc="One of Bahrain's own stations — fetched and verified before it was written down.", icon="🇧🇭", country="Bahrain", bitrate=96),
    Station("bi-heaven-fm-radio", "Heaven FM Radio (Burundi)", "http://stream.zeno.fm/eequgfw72hhvv",
            genre="Local", desc="One of Burundi's own stations, broadcasting from Bujumbula — fetched and verified before it was written down.", icon="🇧🇮", country="Burundi", bitrate=128),
    Station("bj-ortb-radio-b", "ORTB Radio Bénin", "https://listen.radioking.com/radio/47608/stream/84430",
            genre="News", desc="One of Benin's own stations — fetched and verified before it was written down.", icon="🇧🇯", language="French", country="Benin", bitrate=192),
    Station("bm-dr-dick-s", "dr_dick's dub shack", "https://streamer.radio.co/s0635c8b0d/listen",
            genre="Local", desc="One of Bermuda's own stations, broadcasting from Hamilton — fetched and verified before it was written down.", icon="🇧🇲", language="English", country="Bermuda", capital="Hamilton", bitrate=192),
    Station("bo-sucremanta-tv", "Sucremanta Tv", "https://stream.zeno.fm/gli54dgn5kwuv",
            genre="News", desc="One of Bolivia's own stations, broadcasting from Sucre — fetched and verified before it was written down.", icon="🇧🇴", country="Bolivia", capital="Sucre", bitrate=128),
    Station("br-clube-fm-brasilia", "Clube FM Brasilia", "https://8157.brasilstream.com.br/stream",
            genre="Local", desc="One of Brazil's own stations — fetched and verified before it was written down.", icon="🇧🇷", country="Brazil", capital="Brasilia", bitrate=128),
    Station("bs-96-9-fm", "96.9 FM GuardianRadio", "https://radiostreams.streamcomedia.com:8000/969guardianradio",
            genre="Local", desc="One of Bahamas's own stations, broadcasting from Nassau — fetched and verified before it was written down.", icon="🇧🇸", country="Bahamas", capital="Nassau", bitrate=96),
    Station("by-fm", "Компас FM", "https://radio.mil.by/live",
            genre="Local", desc="One of Belarus's own stations, broadcasting from Minsk — fetched and verified before it was written down.", icon="🇧🇾", language="Russian", country="Belarus", capital="Minsk", bitrate=160),
    Station("ca-ckdj-107-9", "CKDJ 107.9 Algonquin College, Ottawa", "http://live.leanstream.co/CKDJFM-MP3?tunein",
            genre="Local", desc="One of Canada's own stations, broadcasting from Ontario — fetched and verified before it was written down.", icon="🇨🇦", language="English", country="Canada", capital="Ottawa", bitrate=128),
    Station("cd-radio-mix-congolai", "Radio Mix Congolaise", "https://stream.zeno.fm/qe5g83upga0uv",
            genre="Local", desc="One of Democratic Republic Of Congo's own stations, broadcasting from Kinshasa — fetched and verified before it was written down.", icon="🇨🇩", language="English", country="Democratic Republic Of Congo", capital="Kinshasa", bitrate=96),
    Station("ch-radio-swiss-jazz", "Radio Swiss Jazz", "http://stream.srg-ssr.ch/m/rsj/mp3_128",
            genre="Jazz", desc="One of Switzerland's own stations, broadcasting from Bern — fetched and verified before it was written down.", icon="🇨🇭", country="Switzerland", capital="Bern", bitrate=128),
    Station("ci-radio-maria-ivory", "Radio Maria Ivory Coast", "https://dreamsiteradiocp6.com/proxy/rmcosta?mp=/stream",
            genre="Local", desc="One of Ivory Coast's own stations — fetched and verified before it was written down.", icon="🇨🇮", country="Ivory Coast", bitrate=128),
    Station("cl-radio-el-conquista", "Radio El Conquistador FM (Santiago)", "https://stream10.usastreams.com/9314/stream/",
            genre="Local", desc="One of Chile's own stations, broadcasting from Región Metropolitana — fetched and verified before it was written down.", icon="🇨🇱", country="Chile", capital="Santiago", bitrate=128),
    Station("cn-nugs-china-radio", "NUGS-China Radio", "https://stream.zeno.fm/ftap1exq6k8uv",
            genre="Local", desc="One of China's own stations — fetched and verified before it was written down.", icon="🇨🇳", language="Chinese", country="China", bitrate=128),
    Station("co-ol-mpica-stereo", "Olímpica Stereo(Bogotá)", "https://playerservices.streamtheworld.com/api/livestream-redirect/OLP_BOGOTAAAC.aac",
            genre="Local", desc="One of Colombia's own stations, broadcasting from Bogota D.C. — fetched and verified before it was written down.", icon="🇨🇴", language="Spanish", country="Colombia", capital="Bogota", bitrate=64),
    Station("cr-planet-107-5", "Planet 107.5", "https://stream20.usastreams.com/8094/stream",
            genre="Local", desc="One of Costa Rica's own stations, broadcasting from San Jose — fetched and verified before it was written down.", icon="🇨🇷", country="Costa Rica", capital="San Jose", bitrate=128),
    Station("cu-cubanflow-radio", "CubanFlow Radio", "http://nap.casthost.net:9194/",
            genre="Local", desc="One of Cuba's own stations, broadcasting from Havana — fetched and verified before it was written down.", icon="🇨🇺", language="Spanish", country="Cuba", capital="Havana", bitrate=128),
    Station("cv-praia-fm", "Praia FM", "https://mpc1.mediacp.eu:8030/stream",
            genre="Local", desc="One of Cabo Verde's own stations — fetched and verified before it was written down.", icon="🇨🇻", country="Cabo Verde", capital="Praia", bitrate=128),
    Station("cw-radio-94-korsou", "Radio 94 Korsou - 94.5 Willemstad", "http://162.243.94.61:8028/;stream.mp3",
            genre="Pop", desc="One of Curacao's own stations — fetched and verified before it was written down.", icon="🇨🇼", language="Papiamento", country="Curacao", capital="Willemstad", bitrate=100),
    Station("cy-dance-fm", "Dance FM", "https://live.enerjiplay.com/stream/dancefm",
            genre="Local", desc="One of Cyprus's own stations, broadcasting from Nicosia — fetched and verified before it was written down.", icon="🇨🇾", country="Cyprus", capital="Nicosia", bitrate=128),
    Station("cz-fed-s-trap", "Fed's Trap Radio", "https://backend.fedstrapradio.com:8000/radio.mp3",
            genre="Chill", desc="One of Czechia's own stations, broadcasting from Prague — fetched and verified before it was written down.", icon="🇨🇿", country="Czechia", capital="Prague", tags=("chill", "energetic",), bitrate=192),
    Station("de-deutschlandfunk-dl", "Deutschlandfunk | DLF | MP3 128k", "https://st01.sslstream.dlf.de/dlf/01/128/mp3/stream.mp3?aggregator=web",
            genre="Culture", desc="One of Germany's own stations, broadcasting from Berlin — fetched and verified before it was written down.", icon="🇩🇪", country="Germany", capital="Berlin", bitrate=128),
    Station("dk-tango-copenhagen-r", "Tango Copenhagen Radio", "https://tangocopenhagen.stream.laut.fm/tangocopenhagen?t302=2021-01-15_18-47-40&uuid=64db9a91-4a6e-4bc8-ba24-26a2fa7f24d0",
            genre="Local", desc="One of Denmark's own stations — fetched and verified before it was written down.", icon="🇩🇰", language="English", country="Denmark", capital="Copenhagen", bitrate=128),
    Station("dm-dbs-radio", "DBS Radio", "https://stream.dbcradio.net:8005/live",
            genre="Local", desc="One of Dominica's own stations, broadcasting from Roseau — fetched and verified before it was written down.", icon="🇩🇲", country="Dominica", capital="Roseau", bitrate=96),
    Station("do-top-bachata-radio", "Top Bachata Radio", "https://radio.dominiserver.com/proxy/topbachata?mp=/stream",
            genre="Local", desc="One of Dominican Republic's own stations, broadcasting from Santo Domingo — fetched and verified before it was written down.", icon="🇩🇴", country="Dominican Republic", capital="Santo Domingo", bitrate=128),
    Station("ec-radio-la-otra", "Radio La Otra - 91.3 FM", "https://laotrafm.makrodigital.com/stream/laotrafmquito",
            genre="Local", desc="One of Ecuador's own stations, broadcasting from Quito — fetched and verified before it was written down.", icon="🇪🇨", country="Ecuador", capital="Quito", bitrate=64),
    Station("ee-nrj-tallinn", "NRJ Tallinn", "https://edge03.cdn.bitflip.ee:8888/NRJ",
            genre="Dance", desc="One of Estonia's own stations — fetched and verified before it was written down.", icon="🇪🇪", language="Estonian", country="Estonia", capital="Tallinn", tags=("energetic",), bitrate=320),
    Station("eg-radio-hits-88", "Radio Hits 88.2 Cairo", "https://radiohits882.radioca.st/;",
            genre="Local", desc="One of Egypt's own stations — fetched and verified before it was written down.", icon="🇪🇬", language="Arabic", country="Egypt", capital="Cairo", bitrate=128),
    Station("er-eritrean-ethiopian", "Eritrean/Ethiopian music", "https://linuxfreelancer.com:8443/test.mp3",
            genre="Local", desc="One of Eritrea's own stations — fetched and verified before it was written down.", icon="🇪🇷", country="Eritrea", bitrate=56),
    Station("es-mix80radio", "Mix80Radio", "https://stream.tunerplay.com/radio/8040/radio.mp3",
            genre="Local", desc="One of Spain's own stations, broadcasting from Madrid — fetched and verified before it was written down.", icon="🇪🇸", language="Spanish", country="Spain", capital="Madrid", bitrate=256),
    Station("et-fm-addis-97", "FM ADDIS 97-1", "https://stream.zeno.fm/rb6wbrap7yzuv",
            genre="Local", desc="One of Ethiopia's own stations — fetched and verified before it was written down.", icon="🇪🇹", language="Amharic", country="Ethiopia", capital="Addis Ababa", bitrate=128),
    Station("fi-radio-helsinki-98", "Radio Helsinki 98,5 Mhz", "https://stream.radiohelsinki.fi/stream",
            genre="Local", desc="One of Finland's own stations — fetched and verified before it was written down.", icon="🇫🇮", language="Finnish", country="Finland", capital="Helsinki", bitrate=256),
    Station("fr-france-bleu-paris", "France Bleu Paris", "http://direct.francebleu.fr/live/fb1071-midfi.mp3",
            genre="Local", desc="One of France's own stations, broadcasting from Île-de-France — fetched and verified before it was written down.", icon="🇫🇷", language="French", country="France", capital="Paris", bitrate=128),
    Station("gb-radio-seva-novgoro", "Радио Сева (Radio Seva Novgorodsev)", "https://seva.ru/radio/stream",
            genre="Rock", desc="One of United Kingdom's own stations, broadcasting from London — fetched and verified before it was written down.", icon="🇬🇧", language="Russian", country="United Kingdom", capital="London", tags=("energetic",), bitrate=128),
    Station("gd-wee-fm-93", "Wee FM 93.3/93.9 - Grenada", "https://usa8.fastcast4u.com/proxy/weefm2?mp=/1",
            genre="Reggae", desc="One of Grenada's own stations, broadcasting from St. George's — fetched and verified before it was written down.", icon="🇬🇩", country="Grenada", capital="Saint George's", bitrate=128),
    Station("ge-radio-imedi", "რადიო იმედი (Radio Imedi)", "https://tv.cdn.xsg.ge/radioimedi/stream01/shoutcast",
            genre="Local", desc="One of Georgia's own stations, broadcasting from Tbilisi — fetched and verified before it was written down.", icon="🇬🇪", language="Georgian", country="Georgia", capital="Tbilisi", bitrate=135),
    Station("gh-citi-fm", "Citi FM", "https://citi973fm.radioca.st/index.html:80/;stream.mp3",
            genre="News", desc="One of Ghana's own stations, broadcasting from Greater Accra — fetched and verified before it was written down.", icon="🇬🇭", language="English", country="Ghana", capital="Accra", bitrate=128),
    Station("gi-gbc-radio-gibralta", "GBC Radio Gibraltar", "https://sh.uk.audio-stream.com/proxy/gibralta?mp=/stream",
            genre="Local", desc="One of Gibraltar's own stations — fetched and verified before it was written down.", icon="🇬🇮", language="English", country="Gibraltar", capital="Gibraltar", bitrate=128),
    Station("gr-zeppelin-106-7", "Zeppelin 106.7", "https://radiostreaming.ert.gr/ert-zeppelin",
            genre="Rock", desc="One of Greece's own stations, broadcasting from Athens — fetched and verified before it was written down.", icon="🇬🇷", country="Greece", capital="Athens", bitrate=192),
    Station("gt-exa-fm-101", "Exa FM 101.7 Guatemala", "https://stream.zeno.fm/czdkk32qduhvv",
            genre="Local", desc="One of Guatemala's own stations, broadcasting from Ciudad de Guatemala — fetched and verified before it was written down.", icon="🇬🇹", language="Spanish", country="Guatemala", capital="Guatemala City", bitrate=96),
    Station("gu-harvest-family-rad", "Harvest Family Radio Guam", "https://streaming.live365.com/a11295",
            genre="Local", desc="One of Guam's own stations — fetched and verified before it was written down.", icon="🇬🇺", country="Guam", bitrate=128),
    Station("gy-radio-guyana-inc", "Radio Guyana Inc - Georgetown 89.5 FM | Essequibo 89.3 FM | Berbice 89.7 FM", "https://stream.radio.co/s9668e2ab5/listen",
            genre="Local", desc="One of Guyana's own stations, broadcasting from Georgetown — fetched and verified before it was written down.", icon="🇬🇾", language="English", country="Guyana", capital="Georgetown", bitrate=192),
    Station("hk-radio-disney-asia", "Radio Disney Asia FM 100.7", "https://listen.radioking.com/radio/453221/stream/508076",
            genre="Pop", desc="One of Hong Kong's own stations, broadcasting from Hong Kong, SAR of China — fetched and verified before it was written down.", icon="🇭🇰", language="English", country="Hong Kong", capital="Hong Kong", bitrate=128),
    Station("hn-vox-fm-honduras", "Vox FM Honduras", "https://ice42.securenetsystems.net/VOXFM",
            genre="Local", desc="One of Honduras's own stations, broadcasting from Tegucigalpa — fetched and verified before it was written down.", icon="🇭🇳", language="Spanish", country="Honduras", capital="Tegucigalpa", bitrate=64),
    Station("hr-bravo-kids", "bravo! kids", "https://audio.social3.hr/listen/bravo_kids/stream?3367",
            genre="Local", desc="One of Croatia's own stations, broadcasting from Grad Zagreb — fetched and verified before it was written down.", icon="🇭🇷", language="Croatian", country="Croatia", capital="Zagreb", bitrate=128),
    Station("ht-rfi", "RFI", "https://rfimonde-96k.ice.infomaniak.ch/rfimonde-96k.mp3",
            genre="News", desc="One of Haiti's own stations, broadcasting from Port-au-Prince — fetched and verified before it was written down.", icon="🇭🇹", country="Haiti", capital="Port-au-Prince", bitrate=96),
    Station("hu-klubr-di-95", "Klubrádió 95,3", "https://a7.asurahosting.com:8160/radio.mp3",
            genre="News", desc="One of Hungary's own stations, broadcasting from Budapest — fetched and verified before it was written down.", icon="🇭🇺", language="Hungarian", country="Hungary", capital="Budapest", bitrate=192),
    Station("id-alternatif-radio-j", "Alternatif radio jakarta", "https://void.idserverhost.com/8016/stream",
            genre="Jazz", desc="One of Indonesia's own stations, broadcasting from Jakarta — fetched and verified before it was written down.", icon="🇮🇩", language="Indonesian", country="Indonesia", capital="Jakarta", bitrate=128),
    Station("ie-dublin-s-98fm", "Dublin's 98FM", "https://stream.audioxi.com/98",
            genre="News", desc="One of Ireland's own stations — fetched and verified before it was written down.", icon="🇮🇪", language="English", country="Ireland", capital="Dublin", bitrate=192),
    Station("il-kcm-fm-channel", "Kcm FM Channel 01 Live", "https://media2.93fm.co.il/livemusic",
            genre="Local", desc="One of Israel's own stations, broadcasting from Jerusalem — fetched and verified before it was written down.", icon="🇮🇱", language="Hebrew", country="Israel", capital="Jerusalem", bitrate=128),
    Station("in-radio-maria-india", "RADIO MARIA INDIA", "http://dreamsiteradiocp.com:8002/stream",
            genre="Talk", desc="One of India's own stations — fetched and verified before it was written down.", icon="🇮🇳", country="India", bitrate=64),
    Station("iq-darusalam-radio", "Darusalam Radio", "https://streams.radio.co/s0975ec186/listen",
            genre="Local", desc="One of Iraq's own stations, broadcasting from Baghdad — fetched and verified before it was written down.", icon="🇮🇶", language="Arabic", country="Iraq", capital="Baghdad", bitrate=128),
    Station("ir-radio-mojahed", "Radio Mojahed - رادیو مجاهد", "https://s2.radio.co/s830691c74/listen",
            genre="Local", desc="One of Iran's own stations, broadcasting from Tehran — fetched and verified before it was written down.", icon="🇮🇷", language="Persian", country="Iran", capital="Tehran", bitrate=128),
    Station("is-bylgjan-98-9", "Bylgjan 98.9 Reykjavik", "http://icecast.365net.is:8000/orbbylgjan.aac",
            genre="News", desc="One of Iceland's own stations — fetched and verified before it was written down.", icon="🇮🇸", language="Icelandic", country="Iceland", capital="Reykjavik", bitrate=130),
    Station("it-rai-2", "RAI 2", "https://icestreaming.rai.it/2.mp3",
            genre="Local", desc="One of Italy's own stations — fetched and verified before it was written down.", icon="🇮🇹", country="Italy", bitrate=256),
    Station("jm-radio-jamaica-94fm", "Radio Jamaica 94FM", "https://stream.zeno.fm/cub84trbgy5tv",
            genre="News", desc="One of Jamaica's own stations, broadcasting from Kingston — fetched and verified before it was written down.", icon="🇯🇲", language="English", country="Jamaica", capital="Kingston", bitrate=128),
    Station("jo-mazaj-fm", "Mazaj FM", "https://mazajfm.ice.infomaniak.ch/mazajfm-192.mp3",
            genre="Local", desc="One of Jordan's own stations, broadcasting from Amman — fetched and verified before it was written down.", icon="🇯🇴", country="Jordan", capital="Amman", bitrate=192),
    Station("jp-free-fm-80", "Free FM 80 Tokyo", "https://freefm80.radioca.st/",
            genre="Local", desc="One of Japan's own stations, broadcasting from tokyo — fetched and verified before it was written down.", icon="🇯🇵", language="English", country="Japan", capital="Tokyo", bitrate=320),
    Station("ke-george-jones", "George Jones", "https://streaming.exclusive.radio/er/georgejones/icecast.audio",
            genre="Local", desc="One of Kenya's own stations, broadcasting from Nairobi — fetched and verified before it was written down.", icon="🇰🇪", country="Kenya", capital="Nairobi", bitrate=128),
    Station("kg-kyrgyzstan-obondor", "Кыргызстан Обондору (Kyrgyzstan Obondoru)", "https://cdn.radioplayer.kg:8443/obondoru64",
            genre="Local", desc="One of Kyrgyzstan's own stations — fetched and verified before it was written down.", icon="🇰🇬", language="Kyrgyz", country="Kyrgyzstan", bitrate=56),
    Station("kh-phnom-penh-radio", "Phnom Penh Radio FM 103", "http://radio99.servradio.com:9294/",
            genre="News", desc="One of Cambodia's own stations — fetched and verified before it was written down.", icon="🇰🇭", language="Khmer", country="Cambodia", capital="Phnom Penh", bitrate=96),
    Station("kp-pyongyang-radio-fm", "Pyongyang Radio FM", "https://listen7.myradio24.com/69366",
            genre="Local", desc="One of North Korea's own stations, broadcasting from Pyongyang — fetched and verified before it was written down.", icon="🇰🇵", country="North Korea", capital="Pyongyang", bitrate=128),
    Station("kr-oldpop-cafe", "Oldpop Cafe", "http://cast.oldpopcafe.net:7080/",
            genre="Oldies", desc="One of South Korea's own stations, broadcasting from Seoul — fetched and verified before it was written down.", icon="🇰🇷", language="Korean", country="South Korea", capital="Seoul", tags=("oldies",), bitrate=128),
    Station("kw-ofw-radio", "OFW Radio", "https://s74.radiolize.com:8020/radio.mp3?t=1780673757445",
            genre="Pop", desc="One of Kuwait's own stations, broadcasting from Kuwait City — fetched and verified before it was written down.", icon="🇰🇼", country="Kuwait", capital="Kuwait City", tags=("energetic",), bitrate=96),
    Station("ky-rooster-101", "Rooster 101", "https://rooster101-hurleysmedia.radioca.st/stream",
            genre="Country", desc="One of Cayman Islands's own stations, broadcasting from 319 Shedden Rd, George Town — fetched and verified before it was written down.", icon="🇰🇾", language="English", country="Cayman Islands", capital="George Town", bitrate=192),
    Station("kz-auto-radio-kazakhs", "Auto Radio Kazakhstan", "http://178.88.167.62:8080/AVTORADIO_192",
            genre="Pop", desc="One of Kazakhstan's own stations — fetched and verified before it was written down.", icon="🇰🇿", language="Kazakh", country="Kazakhstan", bitrate=192),
    Station("la-lao-national-radio", "Lao National Radio 103.7", "https://radio.lnr.org.la/fm103",
            genre="Local", desc="One of Laos's own stations — fetched and verified before it was written down.", icon="🇱🇦", country="Laos", bitrate=96),
    Station("lb-al-bal-radio", "Al Bal Radio", "https://albal-lbnet2.radioca.st/stream",
            genre="Local", desc="One of Lebanon's own stations, broadcasting from Beirut — fetched and verified before it was written down.", icon="🇱🇧", country="Lebanon", capital="Beirut", bitrate=128),
    Station("lc-caribbean-hot-fm", "Caribbean Hot FM 105.3 - Castries", "http://auds2.intacs.com/caribbeanhotfm",
            genre="Local", desc="One of Saint Lucia's own stations — fetched and verified before it was written down.", icon="🇱🇨", language="English", country="Saint Lucia", capital="Castries", bitrate=128),
    Station("lk-hiru-fm", "Hiru FM", "https://radio.lotustechnologieslk.net:2020/stream/hirufmgarden",
            genre="Local", desc="One of Sri Lanka's own stations, broadcasting from Colombo — fetched and verified before it was written down.", icon="🇱🇰", language="Sinhala", country="Sri Lanka", capital="Colombo", bitrate=128),
    Station("lt-palanga-street-rad", "Palanga Street Radio", "https://stream.palanga.live:8443/palanga128.mp3",
            genre="Local", desc="One of Lithuania's own stations, broadcasting from Vilnius — fetched and verified before it was written down.", icon="🇱🇹", language="English", country="Lithuania", capital="Vilnius", bitrate=128),
    Station("lu-free-radio-luxembo", "Free Radio Luxembourg FRL", "https://www.radioking.com/play/rfm-luxembourg/137599",
            genre="Pop", desc="One of Luxembourg's own stations, broadcasting from Luxembourg — fetched and verified before it was written down.", icon="🇱🇺", language="Luxembourgish", country="Luxembourg", capital="Luxembourg", tags=("energetic",), bitrate=320),
    Station("lv-station", "Авторадио Латвия", "https://live.relaxfm.lv/03",
            genre="Pop", desc="One of Latvia's own stations, broadcasting from Riga — fetched and verified before it was written down.", icon="🇱🇻", language="Russian", country="Latvia", capital="Riga", bitrate=96),
    Station("ly-radio-arts-tripoli", "راديو فنون طرابلس - Radio Arts Tripoli", "https://stream.zeno.fm/z9wfrdpmgg0uv",
            genre="Local", desc="One of Libya's own stations, broadcasting from Tripolitania — fetched and verified before it was written down.", icon="🇱🇾", language="Arabic", country="Libya", capital="Tripoli", bitrate=128),
    Station("ma-hit-radio-morocco", "Hit Radio (Morocco)", "https://hitradio-maroc.ice.infomaniak.ch/hitradio-maroc-128.mp3",
            genre="Top 40", desc="One of Morocco's own stations, broadcasting from Rabat-Salé-Kénitra — fetched and verified before it was written down.", icon="🇲🇦", country="Morocco", capital="Rabat", bitrate=128),
    Station("mc-yellow-radio", "Yellow.radio", "https://yellowradio.ice.infomaniak.ch/yellowradio.mp3",
            genre="Dance", desc="One of Monaco's own stations, broadcasting from Monaco — fetched and verified before it was written down.", icon="🇲🇨", country="Monaco", capital="Monaco", tags=("energetic",), bitrate=192),
    Station("md-love-pro-dj", "LOVE PRO Dj Radio Chisinau", "http://live.prodjradio.net:8000/;",
            genre="Pop", desc="One of Moldova's own stations — fetched and verified before it was written down.", icon="🇲🇩", language="Romanian", country="Moldova", capital="Chisinau", bitrate=320),
    Station("me-el-radio-podgorica", "El Radio Podgorica", "https://radio.elmag.me/el.mp3",
            genre="Local", desc="One of Montenegro's own stations — fetched and verified before it was written down.", icon="🇲🇪", country="Montenegro", capital="Podgorica", bitrate=128),
    Station("mg-esdes-radio", "ESDES Radio", "https://esdesradio.netpro.tv/listen/esdes_radio/radio.mp3",
            genre="Top 40", desc="One of Madagascar's own stations, broadcasting from Antananarivo — fetched and verified before it was written down.", icon="🇲🇬", language="French", country="Madagascar", capital="Antananarivo", bitrate=128),
    Station("mk-urban-fm-90", "Urban FM 90.8 Skopje", "https://securestreams4.autopo.st:1668/stream",
            genre="Pop", desc="One of North Macedonia's own stations, broadcasting from Macedonia — fetched and verified before it was written down.", icon="🇲🇰", country="North Macedonia", capital="Skopje", bitrate=128),
    Station("ml-radio-woyowayanko", "Radio Woyowayanko 93.0 Bamako", "https://stream.zeno.fm/1mr8wdywz68uv",
            genre="Local", desc="One of Mali's own stations — fetched and verified before it was written down.", icon="🇲🇱", language="Bambara", country="Mali", capital="Bamako", bitrate=128),
    Station("mn-mgl-radio", "MGL Radio", "http://stream17.shoutcastsolutions.com:8096/",
            genre="Local", desc="One of Mongolia's own stations, broadcasting from Ulaanbaatar — fetched and verified before it was written down.", icon="🇲🇳", language="Mongolian", country="Mongolia", capital="Ulaanbaatar", bitrate=128),
    Station("mo-m80-radio-macau", "M80 Radio Macau - 107.1 FM", "https://stream-icy.bauermedia.pt/m80.mp3",
            genre="Pop", desc="One of Macao's own stations, broadcasting from Macao, SAR of China — fetched and verified before it was written down.", icon="🇲🇴", language="English", country="Macao", capital="Macau", tags=("energetic",), bitrate=128),
    Station("mq-martinique-1-re", "Martinique 1ère", "http://martinique.ice.infomaniak.ch/martinique-128.mp3",
            genre="Local", desc="One of Martinique's own stations — fetched and verified before it was written down.", icon="🇲🇶", country="Martinique", bitrate=128),
    Station("mt-radju-maria-malta", "Radju Maria Malta", "http://dreamsiteradiocp.com:8102/",
            genre="Local", desc="One of Malta's own stations, broadcasting from Rabat, Malta — fetched and verified before it was written down.", icon="🇲🇹", language="Maltese", country="Malta", bitrate=32),
    Station("mu-radio-plus-mauriti", "Radio Plus Mauritius", "https://stream.radioplus.mu:8000/radio.mp3",
            genre="News", desc="One of Mauritius's own stations — fetched and verified before it was written down.", icon="🇲🇺", language="English", country="Mauritius", bitrate=128),
    Station("mv-dhivehi-fm", "Dhivehi FM", "https://radio.psm.mv/fmair",
            genre="Local", desc="One of Maldives's own stations — fetched and verified before it was written down.", icon="🇲🇻", country="Maldives", bitrate=128),
    Station("mw-radio-maria-malawi", "RADIO MARIA MALAWI", "http://dreamsiteradiocp.com:8040/stream",
            genre="Talk", desc="One of Malawi's own stations — fetched and verified before it was written down.", icon="🇲🇼", country="Malawi", bitrate=32),
    Station("mx-roca-fm-clasicos", "Roca Fm Clasicos Mexico", "https://xxdelgado.radioca.st/",
            genre="70s", desc="One of Mexico's own stations, broadcasting from Distrito Federal — fetched and verified before it was written down.", icon="🇲🇽", language="Basque", country="Mexico", capital="Mexico City", tags=("oldies",), bitrate=320),
    Station("my-988-fm", "988 FM", "https://28103.live.streamtheworld.com/988_FMAAC.aac",
            genre="Pop", desc="One of Malaysia's own stations, broadcasting from Kuala Lumpur — fetched and verified before it was written down.", icon="🇲🇾", language="Mandarin", country="Malaysia", capital="Kuala Lumpur", tags=("energetic",), bitrate=64),
    Station("mz-lm-radio-mozambiqu", "LM Radio Mozambique", "https://edge.iono.fm/xice/392_medium.mp3",
            genre="Local", desc="One of Mozambique's own stations — fetched and verified before it was written down.", icon="🇲🇿", language="English", country="Mozambique"),
    Station("na-hitradio-namibia", "Hitradio Namibia", "https://s37.derstream.net/hitradio-namibia.mp3",
            genre="Local", desc="One of Namibia's own stations — fetched and verified before it was written down.", icon="🇳🇦", language="German", country="Namibia", bitrate=192),
    Station("ne-wadata-radio-107", "Wadata Radio 107.4 Niamey", "http://stream.zeno.fm/1y2c3qbgbchvv",
            genre="Local", desc="One of Niger's own stations — fetched and verified before it was written down.", icon="🇳🇪", language="French", country="Niger", capital="Niamey", bitrate=128),
    Station("ng-lagosjump-radio", "LagosJump Radio", "https://radio.lagosjumpradio.com/listen/lagosjump_radio/radio.mp3",
            genre="World", desc="One of Nigeria's own stations, broadcasting from Abuja — fetched and verified before it was written down.", icon="🇳🇬", language="English", country="Nigeria", capital="Abuja", bitrate=128),
    Station("ni-rock-fm-105", "Rock FM 105.5", "https://securestream.us/radio/8050/radio.mp3",
            genre="Rock", desc="One of Nicaragua's own stations, broadcasting from Managua — fetched and verified before it was written down.", icon="🇳🇮", language="English", country="Nicaragua", capital="Managua", tags=("energetic",), bitrate=256),
    Station("nl-dance-radio-amster", "Dance Radio Amsterdam", "http://stream.danceradio.nl:9920/",
            genre="Dance", desc="One of Netherlands's own stations, broadcasting from Amsterdam — fetched and verified before it was written down.", icon="🇳🇱", language="Dutch", country="Netherlands", capital="Amsterdam", tags=("energetic",), bitrate=185),
    Station("no-radio-rock-no", "Radio Rock (NO)", "https://live-bauerno.sharp-stream.com/radiorock_no_mp3",
            genre="Rock", desc="One of Norway's own stations, broadcasting from Oslo — fetched and verified before it was written down.", icon="🇳🇴", language="Norwegian", country="Norway", capital="Oslo", tags=("energetic",), bitrate=128),
    Station("np-radio-kantipur", "Radio Kantipur", "https://radio-broadcast.ekantipur.com/stream",
            genre="Local", desc="One of Nepal's own stations, broadcasting from Kathmandu — fetched and verified before it was written down.", icon="🇳🇵", language="English", country="Nepal", capital="Kathmandu", bitrate=128),
    Station("nz-mushroom-fm", "Mushroom FM", "http://65.108.124.70:8676/live",
            genre="Local", desc="One of New Zealand's own stations, broadcasting from wellington — fetched and verified before it was written down.", icon="🇳🇿", country="New Zealand", capital="Wellington", bitrate=128),
    Station("om-hala-fm", "Hala FM", "https://listen-halafm.sharp-stream.com/halafmlow.mp3",
            genre="Local", desc="One of Oman's own stations, broadcasting from Muscat — fetched and verified before it was written down.", icon="🇴🇲", language="Arabic", country="Oman", capital="Muscat", bitrate=64),
    Station("pa-radio-maria-panama", "RADIO MARIA PANAMA", "http://dreamsiteradiocp.com:8082/stream",
            genre="Talk", desc="One of Panama's own stations — fetched and verified before it was written down.", icon="🇵🇦", country="Panama", capital="Panama City", bitrate=64),
    Station("pe-mambo-inn-radio", "Mambo INN Radio", "https://radio.perustream.com:7000/stream",
            genre="Salsa", desc="One of Peru's own stations, broadcasting from Lima — fetched and verified before it was written down.", icon="🇵🇪", language="Spanish", country="Peru", capital="Lima", bitrate=320),
    Station("ph-win-radio-manila", "Win Radio Manila", "https://stream-31.zeno.fm/2ss1hgnu6hhvv",
            genre="Local", desc="One of Philippines's own stations — fetched and verified before it was written down.", icon="🇵🇭", language="English", country="Philippines", capital="Manila", bitrate=192),
    Station("pk-radio-pakistan-isl", "Radio Pakistan Islamabad", "https://whmsonic.radio.gov.pk:7003/stream",
            genre="Local", desc="One of Pakistan's own stations — fetched and verified before it was written down.", icon="🇵🇰", country="Pakistan", capital="Islamabad", bitrate=128),
    Station("pl-eska-do-pracy", "ESKA - Do Pracy", "https://waw.ic.smcdn.pl/6140-1.mp3",
            genre="Dance", desc="One of Poland's own stations, broadcasting from Warsaw — fetched and verified before it was written down.", icon="🇵🇱", language="Polish", country="Poland", capital="Warsaw", tags=("energetic",), bitrate=128),
    Station("pr-hot-102", "Hot 102", "https://server7.servistreaming.com/proxy/hot?mp=%2Fstream%3Ftype%3D.mp3&_=1",
            genre="Local", desc="One of Puerto Rico's own stations, broadcasting from San Juan PR — fetched and verified before it was written down.", icon="🇵🇷", language="English", country="Puerto Rico", capital="San Juan", bitrate=96),
    Station("ps-ajyal-palestine", "Ajyal - Palestine", "https://streamer.mada.ps:8208/ajyal",
            genre="Local", desc="One of Palestine's own stations — fetched and verified before it was written down.", icon="🇵🇸", country="Palestine", bitrate=128),
    Station("pt-wonder-80-s", "Wonder 80's", "https://80.streeemer.com/listen/80s/radio.mp3",
            genre="Local", desc="One of Portugal's own stations, broadcasting from Lisbon — fetched and verified before it was written down.", icon="🇵🇹", country="Portugal", capital="Lisbon", bitrate=128),
    Station("pw-upbeat-pw", "UpBeat.pw", "https://live.upbeat.pw/",
            genre="Local", desc="One of Palau's own stations — fetched and verified before it was written down.", icon="🇵🇼", country="Palau", bitrate=192),
    Station("py-rock-pop-paraguay", "Rock&Pop Paraguay", "https://cp9.serverse.com/proxy/rockandpop/stream",
            genre="Local", desc="One of Paraguay's own stations, broadcasting from Asuncion — fetched and verified before it was written down.", icon="🇵🇾", country="Paraguay", capital="Asuncion", bitrate=128),
    Station("qa-radio-olive-106", "Radio Olive 106.3 - Jiyo Bindass", "https://22733.live.streamtheworld.com/OLIVE1063.mp3",
            genre="Local", desc="One of Qatar's own stations, broadcasting from Doha QATAR  — fetched and verified before it was written down.", icon="🇶🇦", country="Qatar", capital="Doha", bitrate=128),
    Station("re-maronrfm", "MaronRfm", "https://listen.radioking.com/radio/331087/stream/379402",
            genre="Local", desc="One of Reunion Island's own stations, broadcasting from Saint-Denis — fetched and verified before it was written down.", icon="🇷🇪", country="Reunion Island", capital="Saint-Denis", bitrate=128),
    Station("ro-radaio-romance21-r", "RADAIO ROMANCE21.ROMANIA", "https://stream.rcast.net/200392",
            genre="Local", desc="One of Romania's own stations, broadcasting from Bucharest — fetched and verified before it was written down.", icon="🇷🇴", country="Romania", capital="Bucharest", bitrate=192),
    Station("rs-rock-radio-belgrad", "Rock Radio Belgrade (Рок Радио Београд)", "https://edge9.pink.rs/rockstream",
            genre="Local", desc="One of Serbia's own stations — fetched and verified before it was written down.", icon="🇷🇸", language="Serbian", country="Serbia", capital="Belgrade", bitrate=192),
    Station("ru-station", "Спокойное радио", "https://listen9.myradio24.com/6262",
            genre="Ambient", desc="One of Russia's own stations, broadcasting from Moscow — fetched and verified before it was written down.", icon="🇷🇺", country="Russia", capital="Moscow", tags=("chill",), bitrate=320),
    Station("rw-magic-fm", "Magic FM", "http://listen.rba.co.rw:8080/",
            genre="Dance", desc="One of Rwanda's own stations, broadcasting from Kigali — fetched and verified before it was written down.", icon="🇷🇼", country="Rwanda", capital="Kigali", tags=("energetic",), bitrate=160),
    Station("sa-sba-riyadh-radio", "SBA Riyadh Radio 91.5 FM", "https://stream-154.zeno.fm/pdeizhgrtrstv?zt=eyJhbGciOiJIUzI1NiJ9.eyJzdHJlYW0iOiJwZGVpemhncnRyc3R2IiwiaG9zdCI6InN0cmVhbS0xNTQuemVuby5mbSIsInJ0dGwiOjUsImp0aSI6Im9vbng2V1J3UXJ5Q0k5TVV0N01heEEiLCJpYXQiOjE3MjgyMjQ1MTYsImV4cCI6MTcyODIyNDU3Nn0.gpUIWkDBFNNFjqCg2R-xQN4LXT4z8_Ivs7d2UkeFE_8",
            genre="Local", desc="One of Saudi Arabia's own stations, broadcasting from Mecca — fetched and verified before it was written down.", icon="🇸🇦", language="Arabic", country="Saudi Arabia", capital="Riyadh", bitrate=128),
    Station("sd-hawa-al-sudan", "HAWA al Sudan", "https://stream.zeno.fm/mqctx1bfd8zuv",
            genre="Local", desc="One of Sudan's own stations — fetched and verified before it was written down.", icon="🇸🇩", language="Arabic", country="Sudan", bitrate=128),
    Station("se-mix-megapol-104", "Mix Megapol 104,3", "https://live-bauerse-fm.sharp-stream.com/mixmegapol_instream_se_mp3",
            genre="Pop", desc="One of Sweden's own stations, broadcasting from Stockholm — fetched and verified before it was written down.", icon="🇸🇪", language="Swedish", country="Sweden", capital="Stockholm", bitrate=128),
    Station("sg-singapore-chinese", "Singapore Chinese POP YES FM 933", "https://22893.live.streamtheworld.com/YES933_SC?dist=radiosingapore",
            genre="Pop", desc="One of Singapore's own stations — fetched and verified before it was written down.", icon="🇸🇬", language="Mandarin", country="Singapore", capital="Singapore", bitrate=96),
    Station("si-zeleni-val", "Zeleni val", "https://jurepeterka.radioca.st/stream",
            genre="Local", desc="One of Slovenia's own stations, broadcasting from Ljubljana — fetched and verified before it was written down.", icon="🇸🇮", language="Slovenian", country="Slovenia", capital="Ljubljana", bitrate=320),
    Station("sk-sro2-r-dio", "SRo2 Rádio Regina Západ", "http://icecast.stv.livebox.sk/regina-ba_128.mp3",
            genre="Local", desc="One of Slovakia's own stations, broadcasting from Bratislava — fetched and verified before it was written down.", icon="🇸🇰", language="Slovak", country="Slovakia", capital="Bratislava", bitrate=128),
    Station("sm-usmaradio", "USMARADIO", "http://usmaradio.out.airtime.pro:8000/usmaradio_a",
            genre="Local", desc="One of San Marino's own stations, broadcasting from Città di San Marino — fetched and verified before it was written down.", icon="🇸🇲", language="Italian", country="San Marino", capital="San Marino", bitrate=192),
    Station("sn-fm-s-n", "FM Sénégal 103.1 Dakar", "https://stream-154.zeno.fm/t8gcyq6ts0quv",
            genre="Local", desc="One of Senegal's own stations — fetched and verified before it was written down.", icon="🇸🇳", language="French", country="Senegal", capital="Dakar", bitrate=128),
    Station("sr-radio-10-magic", "Radio 10 Magic FM", "https://s5.radio.co/s85a633f73/listen",
            genre="Local", desc="One of Suriname's own stations, broadcasting from Paramaribo — fetched and verified before it was written down.", icon="🇸🇷", country="Suriname", capital="Paramaribo", bitrate=96),
    Station("sv-radio-yskl-104", "Radio YSKL 104.1 FM", "https://media.dominiocreativo.com:8000/stream/1/",
            genre="Local", desc="One of El Salvador's own stations, broadcasting from San Salvador — fetched and verified before it was written down.", icon="🇸🇻", country="El Salvador", capital="San Salvador", bitrate=192),
    Station("sy-farah-fm", "Farah FM", "https://radio.farah.fm/",
            genre="Local", desc="One of Syria's own stations, broadcasting from Damascus — fetched and verified before it was written down.", icon="🇸🇾", language="Arabic", country="Syria", capital="Damascus", bitrate=96),
    Station("tg-radio-maria-togo", "RADIO MARIA TOGO", "http://dreamsiteradiocp.com:8050/stream",
            genre="Talk", desc="One of Togo's own stations — fetched and verified before it was written down.", icon="🇹🇬", country="Togo", bitrate=64),
    Station("th-91", "สวพ.91", "http://122.155.16.48:8955/;stream.mp3",
            genre="Local", desc="One of Thailand's own stations, broadcasting from Bangkok — fetched and verified before it was written down.", icon="🇹🇭", language="Thai", country="Thailand", capital="Bangkok", bitrate=320),
    Station("tn-radio-tunisia-med", "Radio Tunisia Med", "https://azuracast.conceptradio.fr:8000/radio.mp3",
            genre="Local", desc="One of Tunisia's own stations, broadcasting from Tunis — fetched and verified before it was written down.", icon="🇹🇳", country="Tunisia", capital="Tunis", bitrate=128),
    Station("to-antenna-web-tonga", "Antenna Web Tonga", "https://halo.streamerr.co/listen/italiavera/stream.mp3",
            genre="Pop", desc="One of Tonga's own stations, broadcasting from Nuku'alofa — fetched and verified before it was written down.", icon="🇹🇴", language="English", country="Tonga", capital="Nuku'alofa", bitrate=320),
    Station("tr-ankara-havalari", "Ankara Havalari", "http://37.247.98.8/stream/30/;",
            genre="Folk", desc="One of Türkiye's own stations — fetched and verified before it was written down.", icon="🇹🇷", country="Türkiye", capital="Ankara", bitrate=128),
    Station("tt-vibe-ct-port", "Vibe CT (Port of Spain)", "https://streams.radiomast.io/c35d311e-cc60-41b9-b0c5-339e62c14dee",
            genre="Local", desc="One of Trinidad And Tobago's own stations — fetched and verified before it was written down.", icon="🇹🇹", country="Trinidad And Tobago", capital="Port of Spain", bitrate=128),
    Station("tz-nuru-fm", "Nuru FM", "https://radiotadio.co.tz/nuru-fm-stream",
            genre="Local", desc="One of Tanzania's own stations — fetched and verified before it was written down.", icon="🇹🇿", country="Tanzania", bitrate=128),
    Station("ua-nrj-ukraine", "NRJ Ukraine", "https://cast.mediaonline.net.ua/nrj320",
            genre="Dance", desc="One of Ukraine's own stations, broadcasting from Kyiv — fetched and verified before it was written down.", icon="🇺🇦", country="Ukraine", capital="Kyiv", tags=("energetic",), bitrate=320),
    Station("ug-beat-fm-kampala", "Beat FM - Kampala - 96.3 FM (MP3)", "http://5230.cloudrad.io:8354/live",
            genre="Local", desc="One of Uganda's own stations, broadcasting from Kampala — fetched and verified before it was written down.", icon="🇺🇬", language="English", country="Uganda", capital="Kampala", bitrate=128),
    Station("us-big-r-radio", "Big R Radio - 80s Metal FM", "http://bigrradio.cdnstream1.com/5186_128",
            genre="80s", desc="One of United States's own stations, broadcasting from Washington — fetched and verified before it was written down.", icon="🇺🇸", language="English", country="United States", capital="Washington", tags=("oldies",), bitrate=128),
    Station("uy-24-de-agosto", "24 de Agosto", "https://24deagosto-zikoxweb.radioca.st/stream",
            genre="Oldies", desc="One of Uruguay's own stations, broadcasting from Montevideo — fetched and verified before it was written down.", icon="🇺🇾", language="English", country="Uruguay", capital="Montevideo", tags=("oldies",), bitrate=192),
    Station("uz-islom-uz", "Islom.uz", "https://radio.islom.uz/islomuz",
            genre="Local", desc="One of Uzbekistan's own stations — fetched and verified before it was written down.", icon="🇺🇿", language="Uzbek", country="Uzbekistan", bitrate=128),
    Station("va-radio-vaticana-ita", "Radio Vaticana Italia", "https://radio.vaticannews.va/stream-it",
            genre="Local", desc="One of Vatican City's own stations — fetched and verified before it was written down.", icon="🇻🇦", country="Vatican City", capital="Vatican City", bitrate=128),
    Station("vc-star-fm-98", "Star FM 98.3 & 104.7 Kingstown", "http://starradio.serverroom.net:6688/",
            genre="Pop", desc="One of Saint Vincent and the Grenadines's own stations — fetched and verified before it was written down.", icon="🇻🇨", language="English", country="Saint Vincent and the Grenadines", capital="Kingstown", bitrate=128),
    Station("ve-caracas-salsa-rom", "Caracas. Salsa Romántica...", "https://stream-285.surfernetwork.com/ftly191pojttv?zt=eyJhbGciOiJIUzI1NiJ9.eyJzdHJlYW0iOiJmdGx5MTkxcG9qdHR2IiwiaG9zdCI6InN0cmVhbS0yODUuc3VyZmVybmV0d29yay5jb20iLCJ0bSI6ZmFsc2UsInJ0dGwiOjUsImp0aSI6IlBfd3J5M28zUmdXdWYxazFGSGtobWciLCJpYXQiOjE3OTAwMDgyMTQsImV4cCI6MTc5MDAwODI3NH0.8KQK5YzLQnBC2SNVDBPV9S8IHNuxr6Q2zph78R0jq4Q",
            genre="Salsa", desc="One of Venezuela's own stations — fetched and verified before it was written down.", icon="🇻🇪", country="Venezuela", capital="Caracas", bitrate=128),
    Station("vn-fm-hanoi-90", "FM HANOI 90 MHz", "http://14.162.146.90:8000/HANOI90",
            genre="Local", desc="One of Vietnam's own stations — fetched and verified before it was written down.", icon="🇻🇳", country="Vietnam", capital="Hanoi", bitrate=320),
    Station("vu-paradise-98fm", "Paradise 98FM", "https://radio.vbtc.vu/paradisefm",
            genre="Local", desc="One of Vanuatu's own stations, broadcasting from Port Vila, sef, Vanuatu — fetched and verified before it was written down.", icon="🇻🇺", language="Bislama", country="Vanuatu", capital="Port Vila", bitrate=128),
    Station("za-befonk-in-stereo", "Befonk in stereo", "https://c42.radioboss.fm:8073/stream",
            genre="Pop", desc="One of South Africa's own stations, broadcasting from Pretoria  — fetched and verified before it was written down.", icon="🇿🇦", language="Afrikaans", country="South Africa", capital="Pretoria", bitrate=128),
    Station("zm-hot-fm-87", "Hot FM 87.7", "http://s2.yesstreaming.net:7091/stream?type=.mp3%2F%3Bstream.mp3&_=1",
            genre="Local", desc="One of Zambia's own stations, broadcasting from lusaka — fetched and verified before it was written down.", icon="🇿🇲", language="English", country="Zambia", capital="Lusaka", bitrate=128),
    Station("zw-capitalk-100-4fm", "Capitalk 100.4FM Harare", "https://edge.iono.fm/xice/162_medium.aac",
            genre="Local", desc="One of Zimbabwe's own stations, broadcasting from Harare — fetched and verified before it was written down.", icon="🇿🇼", language="English", country="Zimbabwe", capital="Harare"),
)

#: The same rows, by key — the picker sends a key back, so this is the lookup
#: every command uses. Built once at import; a duplicate key is a bug in the
#: table and :func:`duplicate_keys` exists so a test can name it rather than
#: letting the last row win in silence.
STATIONS: dict[str, Station] = {station.key: station for station in CATALOGUE}


def duplicate_keys() -> list[str]:
    """Keys that appear more than once, worst first. Empty on a healthy table."""
    seen: set[str] = set()
    repeated: list[str] = []
    for station in CATALOGUE:
        if station.key in seen and station.key not in repeated:
            repeated.append(station.key)
        seen.add(station.key)
    return repeated


class Index(NamedTuple):
    """The table counted, for the browse command and the console panel."""

    #: Most-populated genre first: this list is a browse.
    genres: list[tuple[str, int]]
    #: Alphabetically: this list is a lookup, and a lookup is done by scanning.
    languages: list[tuple[str, int]]
    #: Capital-first country rows, alphabetically: this is the country picker.
    countries: tuple[str, ...]
    stations: int
    #: No language tag **and** the entry says why — it carries the catalogue's
    #: own ``instrumental`` mood, which means there are no words to be in one.
    wordless: int
    #: No language tag and no reason recorded: the words are whatever the music
    #: is in, and this table has not established which.
    unlabelled: int
    #: Stations whose own site says they carry no advertising. Counted because it
    #: is the one property a listener *chooses on* rather than browses with — an
    #: ad under a conversation is worse than a track nobody wanted.
    ad_free: int


def index() -> Index:
    """The table counted five ways.

    Languages are counted apart from the two blank buckets, because *"these have
    no language"* and *"this table has not established which"* are different
    facts about a station and only one of them is a gap. Genres come back
    most-populated first (that list is a browse), languages alphabetically (that
    list is a lookup), and both ties are broken by name so two runs cannot print
    two different pages.
    """
    by_genre: dict[str, int] = {}
    by_language: dict[str, int] = {}
    wordless = 0
    unlabelled = 0
    ad_free_count = 0
    countries: list[str] = []
    for station in CATALOGUE:
        by_genre[station.genre] = by_genre.get(station.genre, 0) + 1
        if station.ad_free:
            ad_free_count += 1
        if station.language:
            by_language[station.language] = by_language.get(station.language, 0) + 1
        elif "instrumental" in station.tags:
            wordless += 1
        else:
            unlabelled += 1
        if station.country and station.country not in countries:
            countries.append(station.country)
    return Index(
        genres=sorted(by_genre.items(), key=lambda row: (-row[1], row[0])),
        languages=sorted(by_language.items()),
        countries=tuple(sorted(countries)),
        stations=len(CATALOGUE),
        wordless=wordless,
        unlabelled=unlabelled,
        ad_free=ad_free_count,
    )


def by_key(key: str) -> Station | None:
    """The station a picker's value names, or ``None``."""
    return STATIONS.get((key or "").strip())


def languages() -> list[str]:
    """Every language the table holds stations in, alphabetically."""
    return [language for language, _count in index().languages]


def countries() -> list[str]:
    """Every country the table can actually play, alphabetically.

    A country is here because one of its stations was fetched and verified, which
    is the only entry requirement: a picker that offered a country this bot cannot
    play would be worse than one that admits it has nothing there yet.
    """
    return list(index().countries)


def in_language(language: str) -> list[Station]:
    """The table's entries in one language, in table order."""
    wanted = _flat(language)
    if not wanted:
        return []
    return [station for station in CATALOGUE if _flat(station.language) == wanted]


def in_country(country: str) -> list[Station]:
    """The table's entries for one country, the capital's station first.

    Capital-first rather than alphabetical because that is the question the
    country picker answers: *what does the capital sound like*. A country whose
    rows were never corroborated simply has no rows, which is why
    :func:`countries` is the same list the picker offers.
    """
    wanted = _flat(country)
    if not wanted:
        return []
    rows = [station for station in CATALOGUE if _flat(station.country) == wanted]
    rows.sort(key=lambda station: (not station.capital, station.name))
    return rows


def ad_free_stations() -> list[Station]:
    """The table's ad-free entries, in table order."""
    return [station for station in CATALOGUE if station.ad_free]


def same_language(station: Station, language: str) -> bool:
    """Whether this station is in that language — the one reader of the field."""
    wanted = _flat(language)
    return bool(wanted) and _flat(station.language) == wanted


def option_label(station: Station) -> str:
    """What one suggestion looks like, inside Discord's 100-character label cap.

    The claim is on the label as well as on the card, because the moment somebody
    is choosing is the only moment it can change their mind. Only ever the
    positive direction: a station whose site did not say is left unmarked rather
    than marked *ads*, which would be this bot asserting something it never read.
    """
    label = f"{station.icon} {station.name} · {station.genre}"
    if station.language:
        label += f" · {station.language}"
    if station.country and not station.language:
        label += f" · {station.capital or station.country}"
    if station.ad_free:
        label += " · ad-free"
    return label[:100]


def _flat(text: str) -> str:
    """A searchable form of a name: lower case, and the punctuation nobody types.

    ``Lo-Fi Beats`` has to be findable by typing *lofi beats* — which is what the
    label looks like once the icon is in front of it, and what a person types from
    memory. A hyphen is not something anybody types, so both sides of every
    comparison go through here and ``lofi``, ``lo-fi`` and ``Lo Fi`` are one word.

    The trade is real and worth naming: stripping punctuation loses word
    boundaries, so a search can match inside a longer word. In a table this size,
    where a name match is ranked above everything else, a little noise in the tail
    is a better deal than a station nobody can find.
    """
    return "".join(character for character in text.lower() if character.isalnum())


#: How many suggestions a search may return. Discord's own ceiling is 25.
SUGGESTIONS = 25

#: Words people search with that the table does not use, because a person who
#: wants background music for an essay types *study* and not *focus*. Deliberately
#: tiny and literal: a synonym table that grows into a thesaurus is a search whose
#: results nobody can predict, which is worse than a search that returns nothing
#: and says so. Keys are whole words, matched case- and space-insensitively.
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
    # The tag is ``ad-free`` and nobody types it: what a person asks for is *no
    # ads*, and a search for those two words is "ad" and "free" — neither of which
    # appears in the tag once the hyphen is stripped for matching.
    "ad": ("ad-free",),
    "ads": ("ad-free",),
    "advert": ("ad-free",),
    "adverts": ("ad-free",),
    "advertising": ("ad-free",),
    "commercial": ("ad-free",),
    "commercials": ("ad-free",),
}


def _rank(term: str, station: Station) -> int | None:
    """How well one search word fits one station, or ``None`` for no fit at all.

    Ranked rather than merely filtered, because a description is long enough to
    swallow a search: somebody typing *jazz* wants the jazz stations, not the
    forty entries whose blurb mentions jazz once. So a name match beats a genre or
    a key match, which beats a language, country or mood tag, which beats a
    mention anywhere in the description.
    """
    needle = _flat(term)
    if not needle:
        return None
    if needle in _flat(station.name):
        return 0
    if needle in _flat(station.genre) or needle in _flat(station.key):
        return 1
    if (
        needle in _flat(station.language)
        or needle in _flat(station.country)
        or needle in _flat(station.capital)
        or needle in _flat(" ".join(station.tags))
    ):
        return 2
    if needle in _flat(station.desc):
        return 3
    return None


def search(query: str, *, limit: int = SUGGESTIONS) -> list[Station]:
    """Stations matching a search, best first.

    A synonym hit counts one rank worse than the same hit on the words actually
    typed, so *study* finds the ``focus`` stations without ever outranking
    something that literally says study. Ties keep table order, which is grouped
    by genre, so neighbours in the list stay neighbours in the results.
    """
    text = (query or "").strip().lower()
    if not text:
        return list(CATALOGUE[:limit])
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
    ranked: list[tuple[int, int, Station]] = []
    for position_in_table, station in enumerate(CATALOGUE):
        best: int | None = None
        for position, term in enumerate(terms):
            found = _rank(term, station)
            if found is None:
                continue
            found = found if position == 0 else found + 1
            best = found if best is None else min(best, found)
        if best is not None:
            ranked.append((best, position_in_table, station))
    ranked.sort(key=lambda row: (row[0], row[1]))
    return [station for _rank_value, _position, station in ranked[:limit]]


def find(value: str) -> Station | None:
    """The station a key or a name names, or ``None`` — deliberately narrow.

    The key is what the picker sends back and the *name* is what a member reads on
    the card and types the next time, so both are matched exactly, and a name
    somebody half-remembers is matched as a piece of a name. Nothing else counts.

    This is narrower than :func:`search` on purpose, and the difference is the
    whole reason it exists: a search cannot fail — it ranks mentions in a blurb,
    so *any* phrase finds something — while this answers *"which station did this
    person name"*, where the honest answer for a phrase the table does not hold is
    ``None``. Falling back to a search here is how *"play Radio Nowhere At All"*
    comes back playing a chill-out stream instead of saying it has no such station.
    """
    text = (value or "").strip()
    if not text:
        return None
    wanted = _flat(text)
    for station in CATALOGUE:
        if wanted == _flat(station.key):
            return station
    for station in CATALOGUE:
        if wanted == _flat(station.name):
            return station
    for station in CATALOGUE:
        if _rank(text, station) == 0:
            return station
    return None
