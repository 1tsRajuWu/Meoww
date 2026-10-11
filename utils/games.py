"""Curated free and ad-free game repository and directory.

Provides verified 100% free and 100% AD-FREE game destinations, web-playable
classics, open-source games, and verified zero-ad distribution platforms
(Itch.io, Internet Archive, Lichess, Steam Free, Epic Games Freebies, GOG).
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True)
class GameInfo:
    title: str
    category: str
    description: str
    platform_type: str
    ad_free_guarantee: str
    primary_url: str
    primary_label: str
    secondary_url: str | None = None
    secondary_label: str | None = None
    extra_sources: tuple[tuple[str, str], ...] = ()


# Curated catalog of verified 100% free, 100% AD-FREE games and platforms.
CATALOG: dict[str, GameInfo] = {
    "minecraft": GameInfo(
        title="Minecraft (Free & Ad-Free Libre Editions)",
        category="Voxel Sandbox",
        description=(
            "Experience infinite voxel worlds, mining, crafting, survival, and multiplayer "
            "completely free with zero advertisements or paywalls."
        ),
        platform_type="Web Browser & PC / Mac / Linux",
        ad_free_guarantee="100% Free · Open-Source · Zero Ads · Zero Popups",
        primary_url="https://www.luanti.org/",
        primary_label="⛏️ Play Luanti (Minetest)",
        secondary_url="https://classicube.net/",
        secondary_label="🧱 Play ClassiCube (Web & Desktop)",
        extra_sources=(
            ("Eaglercraft (Browser 1.8.8)", "https://eaglercraft.com/"),
        ),
    ),
    "doom": GameInfo(
        title="DOOM (1993 Classic & FreeDoom)",
        category="Classic FPS",
        description=(
            "The iconic fast-paced shooter running directly in your modern web browser "
            "via WebAssembly and Internet Archive emulation, plus the 100% open-source FreeDoom."
        ),
        platform_type="Instant In-Browser Play & PC",
        ad_free_guarantee="100% Free · Zero Ads · Full Speed WebAssembly",
        primary_url="https://silviolq.github.io/doom/",
        primary_label="🔫 Play DOOM in Browser",
        secondary_url="https://freedoom.github.io/",
        secondary_label="💥 FreeDoom (Open Source)",
        extra_sources=(
            ("Internet Archive DOOM", "https://archive.org/details/doom-play"),
        ),
    ),
    "pokemon": GameInfo(
        title="Pokémon (PokeRogue & Pokémon Showdown)",
        category="Creature Collector & RPG",
        description=(
            "Play the hit roguelite RPG **PokeRogue** (gyms, biomes, bosses, shiny hunting) "
            "or competitive battles on **Pokémon Showdown** with zero ads and zero microtransactions."
        ),
        platform_type="Instant Web Browser Play (Mobile & Desktop)",
        ad_free_guarantee="100% Free · Community Powered · Zero Ads · Zero Paywalls",
        primary_url="https://pokerogue.net/",
        primary_label="⚡ Play PokeRogue (Ad-Free)",
        secondary_url="https://pokemonshowdown.com/",
        secondary_label="⚔️ Pokémon Showdown Battles",
    ),
    "chess": GameInfo(
        title="Lichess (The Free, Ad-Free Chess Server)",
        category="Board Game & Strategy",
        description=(
            "The world's leading open-source chess platform. Unlimited puzzles, Stockfish cloud "
            "computer analysis, rated multiplayer, tournaments, and lessons with zero ads forever."
        ),
        platform_type="Web Browser, iOS & Android",
        ad_free_guarantee="100% Free Forever · No Ads · No Subscriptions · Open Source",
        primary_url="https://lichess.org/",
        primary_label="♟️ Play on Lichess",
        secondary_url="https://lichess.org/practice",
        secondary_label="🎓 Free Chess Lessons & Puzzles",
    ),
    "tetris": GameInfo(
        title="TETR.IO & Jstris (Modern Ad-Free Block Stacking)",
        category="Arcade & Puzzle",
        description=(
            "High-speed, buttery-smooth block stacking puzzle action with customizable controls, "
            "solo 40-lines sprint, sprint leaderboards, and real-time multiplayer lobbies."
        ),
        platform_type="Web Browser & Desktop",
        ad_free_guarantee="100% Free · Zero Ads · Ultra-Low Latency",
        primary_url="https://tetr.io/",
        primary_label="🧱 Play TETR.IO",
        secondary_url="https://jstris.jezevec10.com/",
        secondary_label="⚡ Play Jstris (Instant)",
    ),
    "celeste": GameInfo(
        title="Celeste Classic (PICO-8 Original)",
        category="Precision Platformer",
        description=(
            "The original award-winning PICO-8 precision platformer created by Maddy Thorson and "
            "Noel Berry. Dash, climb, and reach the summit in your browser with zero ads."
        ),
        platform_type="Instant In-Browser Play",
        ad_free_guarantee="100% Free · Official Indie Release · Zero Ads",
        primary_url="https://mattmakesgames.itch.io/celeste-classic",
        primary_label="🏔️ Play Celeste Classic",
    ),
    "retro": GameInfo(
        title="Internet Archive MS-DOS & Retro Games",
        category="Classic & Retro Gaming",
        description=(
            "Over 4,000 historic MS-DOS and arcade games preserved legally and playable "
            "straight inside any modern browser with EM-DOSBox. Prince of Persia, SimCity, "
            "Oregon Trail, Wolfenstein 3D, and more."
        ),
        platform_type="Instant In-Browser EM-DOSBox Emulation",
        ad_free_guarantee="100% Free · Historical Non-Profit · Zero Ads · Zero Popups",
        primary_url="https://archive.org/details/softwarelibrary_msdos_games",
        primary_label="🕹️ Explore 4,000+ Retro Games",
        secondary_url="https://archive.org/details/internetarcade",
        secondary_label="👾 Internet Arcade Classics",
    ),
    "puzzle": GameInfo(
        title="Simon Tatham's Puzzles & Neal.fun",
        category="Logic & Puzzle Games",
        description=(
            "40 standalone, clean mathematical and logic puzzles (Sudoku, Minesweeper, Net, Bridges, "
            "Light Up, Pattern) by Simon Tatham with zero trackers or ads, plus Neal.fun's hit sandboxes."
        ),
        platform_type="Web Browser & Cross-Platform",
        ad_free_guarantee="100% Free · Open-Source · Zero Ads · Zero Trackers",
        primary_url="https://www.chiark.greenend.org.uk/~sgtatham/puzzles/",
        primary_label="🧩 Simon Tatham's 40 Puzzles",
        secondary_url="https://neal.fun/",
        secondary_label="✨ Neal.fun (Infinite Craft & Games)",
    ),
    "horror": GameInfo(
        title="Indie Horror (Itch.io Free & Cry of Fear)",
        category="Survival Horror & Thriller",
        description=(
            "Explore thousands of atmosphere-rich, psychological and retro PS1-style indie horror "
            "games hosted on Itch.io, plus cult classic survival horrors like Cry of Fear on Steam."
        ),
        platform_type="PC / Mac / Linux & Browser",
        ad_free_guarantee="100% Free · DRM-Free · Zero Third-Party Ads",
        primary_url="https://itch.io/games/free/tag-horror",
        primary_label="👻 Browse Itch.io Free Horror",
        secondary_url="https://store.steampowered.com/app/223710/Cry_of_Fear/",
        secondary_label="🔦 Cry of Fear on Steam (Free)",
    ),
    "rpg": GameInfo(
        title="Free RPGs & Roguelikes (Dungeon Crawl & Itch.io)",
        category="Role Playing & Roguelike",
        description=(
            "Deep tactical turn-based RPG exploration in Dungeon Crawl Stone Soup (playable in browser "
            "or offline), Brogue community edition, and hundreds of DRM-free indie RPGs on Itch.io."
        ),
        platform_type="Web Browser & Desktop",
        ad_free_guarantee="100% Free · Open-Source & DRM-Free · Zero Ads",
        primary_url="https://crawl.develz.org/",
        primary_label="⚔️ Play Dungeon Crawl (DCSS)",
        secondary_url="https://itch.io/games/free/tag-rpg",
        secondary_label="🛡️ Itch.io Free RPG Collection",
        extra_sources=(
            ("Brogue Community Edition", "https://brogue.community/"),
        ),
    ),
    "fps": GameInfo(
        title="Open-Source Arena Shooters (Xonotic & QuakeJS)",
        category="First-Person Shooter",
        description=(
            "High-octane, arena multiplayer shooters with crisp movement, weapons, and community "
            "servers. Xonotic is completely free and open-source; QuakeJS runs in-browser."
        ),
        platform_type="PC / Mac / Linux & Browser",
        ad_free_guarantee="100% Free · Libre / Open Source · Zero Commercial Ads",
        primary_url="https://xonotic.org/",
        primary_label="🔫 Download Xonotic Arena",
        secondary_url="http://www.quakejs.com/",
        secondary_label="⚡ Play QuakeJS in Browser",
    ),
    "strategy": GameInfo(
        title="0 A.D. & Battle for Wesnoth & OpenRA",
        category="Real-Time & Turn-Based Strategy",
        description=(
            "Top-tier RTS and tactical games: **0 A.D.** (3D ancient civilizations RTS like Age of Empires), "
            "**OpenRA** (Command & Conquer / Red Alert / Dune 2000), and **Battle for Wesnoth** (turn-based fantasy)."
        ),
        platform_type="PC / Mac / Linux (Cross-Platform)",
        ad_free_guarantee="100% Free & Open-Source · Zero Ads · Zero Microtransactions",
        primary_url="https://play0ad.com/",
        primary_label="🏰 Play 0 A.D. (Historical RTS)",
        secondary_url="https://www.openra.net/",
        secondary_label="🚀 OpenRA (C&C & Red Alert)",
        extra_sources=(
            ("Battle for Wesnoth", "https://www.wesnoth.org/"),
            ("Freeciv in Browser", "https://www.freecivweb.org/"),
        ),
    ),
    "racing": GameInfo(
        title="SuperTuxKart (3D Mascot Kart Racing)",
        category="Arcade Racing",
        description=(
            "Rich 3D kart racing featuring open-source mascots, grand prix story mode, drift mechanics, "
            "weapons, battle arenas, and online cross-platform multiplayer. Completely free with zero ads."
        ),
        platform_type="PC, Mac, Linux & Android",
        ad_free_guarantee="100% Free · Open-Source · Zero Ads · Zero In-App Purchases",
        primary_url="https://supertuxkart.net/",
        primary_label="🏎️ Download SuperTuxKart",
    ),
    "action": GameInfo(
        title="Free Action & Platformer Games",
        category="Action & Arcade",
        description=(
            "Fast-paced action and platforming games verified ad-free. Download SuperTux, "
            "explore Itch.io's curated free action category, or jump into browser-playable retro classics."
        ),
        platform_type="Browser & PC",
        ad_free_guarantee="100% Free · Zero Ads · DRM-Free Downloads",
        primary_url="https://itch.io/games/free/tag-action",
        primary_label="💥 Itch.io Free Action Games",
        secondary_url="https://www.supertux.org/",
        secondary_label="🐧 SuperTux 2 (Free Platformer)",
    ),
    "sonic": GameInfo(
        title="Sonic Robo Blast 2 (SRB2)",
        category="3D Platformer",
        description=(
            "A fan-made 3D Sonic platformer built on a modified Doom engine with huge levels, "
            "momentum-based physics, secrets, and multiplayer. Completely free, non-commercial, and ad-free."
        ),
        platform_type="PC / Mac / Linux / Android",
        ad_free_guarantee="100% Free Fan Project · Zero Ads · Zero Profit",
        primary_url="https://www.srb2.org/",
        primary_label="🦔 Download Sonic Robo Blast 2",
    ),
    "mario": GameInfo(
        title="Classic Mario Emulation & Fan Games",
        category="Platformer & Retro",
        description=(
            "Play preserved classic platforming games legally in your browser through the Internet Archive "
            "software library with zero ads and zero intrusive popups."
        ),
        platform_type="Instant Web Browser Play",
        ad_free_guarantee="100% Free · Preserved Emulation · Zero Ads",
        primary_url="https://archive.org/details/softwarelibrary_msdos_games?query=mario",
        primary_label="🍄 Play Mario on Internet Archive",
    ),
    "zelda": GameInfo(
        title="Zelda Classic & Solarus Engine",
        category="Action-Adventure RPG",
        description=(
            "Explore classic 2D top-down Zelda adventures with **Zelda Classic** and the open-source "
            "**Solarus ARPG Engine** featuring full custom community-created quests with zero ads."
        ),
        platform_type="PC / Mac / Linux",
        ad_free_guarantee="100% Free Community Project · Zero Ads · Zero Microtransactions",
        primary_url="https://www.zeldaclassic.com/",
        primary_label="🗡️ Zelda Classic",
        secondary_url="https://www.solarus-games.org/",
        secondary_label="🛡️ Solarus Open-Source ARPG",
    ),
    "rhythm": GameInfo(
        title="osu! (Rhythm is just a click away)",
        category="Rhythm & Music",
        description=(
            "The community-favorite rhythm game with hundreds of thousands of user-created beatmaps, "
            "multiplayer lobbies, competitive rankings, and zero third-party ads."
        ),
        platform_type="PC / Mac / Linux / Mobile",
        ad_free_guarantee="100% Free · Community Powered · Zero Third-Party Ads",
        primary_url="https://osu.ppy.sh/",
        primary_label="🎵 Download osu!",
    ),
    "epic": GameInfo(
        title="Epic Games Store Weekly Free Commercial Games",
        category="Commercial AAA & Indie Giveaways",
        description=(
            "Epic Games gives away 1 to 2 full commercial AAA and indie PC games completely free "
            "every Thursday. Claim them to keep them in your library forever with zero ads."
        ),
        platform_type="PC / Mac via Epic Games Launcher",
        ad_free_guarantee="100% Free Official Giveaways · Keep Forever · Zero In-Game Ads",
        primary_url="https://store.epicgames.com/free-games",
        primary_label="🎁 Epic Games Weekly Freebies",
    ),
    "gog": GameInfo(
        title="GOG.com 100% Free DRM-Free Games",
        category="Classic & Indie DRM-Free",
        description=(
            "GOG's permanent free collection features classic and modern games with zero DRM, "
            "offline standalone installers, and zero advertising."
        ),
        platform_type="PC / Mac / Linux Standalone Installers",
        ad_free_guarantee="100% Free · 100% DRM-Free · Zero Ads · Offline Installers",
        primary_url="https://www.gog.com/games?price=free",
        primary_label="💾 GOG Free DRM-Free Hub",
    ),
    "steam": GameInfo(
        title="Steam Free-to-Play & 100% Free Library",
        category="PC Gaming Store Hub",
        description=(
            "The official Steam store free-to-play hub featuring verified titles like Counter-Strike 2, "
            "Team Fortress 2, Destiny 2, Apex Legends, Path of Exile, and thousands of indie games."
        ),
        platform_type="PC / Mac / Steam Deck",
        ad_free_guarantee="Official Steam Store · Safe & Verified Downloads · Zero Popup Ads",
        primary_url="https://store.steampowered.com/genre/Free%20to%20Play/",
        primary_label="🌌 Steam Free-to-Play Hub",
    ),
}

# Aliases to help natural queries reach curated catalog items
ALIASES: dict[str, str] = {
    "mine craft": "minecraft",
    "craft": "minecraft",
    "minetest": "minecraft",
    "luanti": "minecraft",
    "classicube": "minecraft",
    "eagler": "minecraft",
    "freedoom": "doom",
    "fps": "fps",
    "shooter": "fps",
    "gun": "fps",
    "pokerogue": "pokemon",
    "showdown": "pokemon",
    "poke": "pokemon",
    "tetrio": "tetris",
    "jstris": "tetris",
    "puzzles": "puzzle",
    "logic": "puzzle",
    "neal": "puzzle",
    "infinite craft": "puzzle",
    "scary": "horror",
    "spooky": "horror",
    "rpgs": "rpg",
    "roguelike": "rpg",
    "dcss": "rpg",
    "rts": "strategy",
    "war": "strategy",
    "age of empires": "strategy",
    "openra": "strategy",
    "0ad": "strategy",
    "0 a.d.": "strategy",
    "karts": "racing",
    "car": "racing",
    "cars": "racing",
    "supertuxkart": "racing",
    "platformer": "action",
    "supertux": "action",
    "srb2": "sonic",
    "retro games": "retro",
    "msdos": "retro",
    "dos": "retro",
    "arcade": "retro",
    "osu": "rhythm",
    "music": "rhythm",
    "epic games": "epic",
    "giveaway": "epic",
    "freebies": "epic",
    "gog.com": "gog",
    "drm free": "gog",
    "steam free": "steam",
    "f2p": "steam",
}


def lookup_game(query: str) -> GameInfo:
    """Find a verified ad-free game or generate a tailored ad-free directory."""
    raw = (query or "").strip().lower()
    cleaned = "".join(c for c in raw if c.isalnum() or c.isspace()).strip()

    # Direct match or alias match
    key = ALIASES.get(cleaned) or ALIASES.get(raw) or cleaned
    if key in CATALOG:
        return CATALOG[key]

    # Partial substring match against catalog keys
    for cat_key, info in CATALOG.items():
        if cat_key in cleaned or cleaned in cat_key:
            return info

    # Fallback: Dynamic tailored search on verified 100% ad-free & legal sources
    encoded = urllib.parse.quote_plus(query.strip())
    title_display = query.strip().title()

    itch_url = f"https://itch.io/search?q={encoded}&price=free"
    steam_url = f"https://store.steampowered.com/search/?maxprice=free&term={encoded}"
    archive_url = f"https://archive.org/details/softwarelibrary_msdos_games?query={encoded}"
    gog_url = f"https://www.gog.com/games?price=free&query={encoded}"

    return GameInfo(
        title=f"'{title_display}' (Free & Ad-Free Game Search)",
        category="Curated Ad-Free Directory",
        description=(
            f"Here are the top verified, 100% free and **AD-FREE** platforms to play or download "
            f"**{title_display}** (or similar games in that genre) with zero ads, popups, or malware."
        ),
        platform_type="Web Browser & PC / Mac / Linux",
        ad_free_guarantee="🛡️ 100% Ad-Free Guarantee · Zero Popups · Safe & Legal Sources",
        primary_url=itch_url,
        primary_label=f"🕹️ Search '{title_display}' on Itch.io (Free)",
        secondary_url=steam_url,
        secondary_label="🌌 Search on Steam (100% Free)",
        extra_sources=(
            ("💾 Internet Archive MS-DOS Library", archive_url),
            ("🎮 GOG Free DRM-Free Catalog", gog_url),
            ("🎁 Epic Games Weekly Free Commercial Games", "https://store.epicgames.com/free-games"),
        ),
    )


def popular_suggestions(current: str) -> list[str]:
    """Provide smart autocomplete choices for Discord slash command input."""
    base = [
        "Minecraft (Minetest & ClassiCube)",
        "DOOM (Classic Web & FreeDoom)",
        "Pokémon (PokeRogue & Showdown)",
        "Chess (Lichess 100% Ad-Free)",
        "TETR.IO (Modern Web Tetris)",
        "Horror (Itch.io Free & Cry of Fear)",
        "Retro & MS-DOS (4,000+ Games)",
        "Puzzles (Simon Tatham 40 Puzzles)",
        "RPG & Roguelikes (Dungeon Crawl)",
        "FPS & Arena Shooters (Xonotic)",
        "Strategy & RTS (0 A.D. & OpenRA)",
        "Celeste Classic (PICO-8 Web)",
        "SuperTuxKart (3D Racing)",
        "Epic Games Weekly Freebies",
        "GOG Free DRM-Free Games",
        "Steam Free-to-Play Hub",
    ]
    cur = current.strip().lower()
    if not cur:
        return base[:15]
    matches = [s for s in base if cur in s.lower()]
    if not matches:
        # If no canned item matches, offer custom search options
        matches = [f"Search free '{current}' on Itch.io", f"Search free '{current}' on Steam"]
    return matches[:15]
