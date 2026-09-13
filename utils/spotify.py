"""
Spotify Link Resolver for Discord Music Bot.
Resolves Spotify tracks, albums, and playlists into searchable queries (Title + Artist)
and rich metadata without requiring any Spotify Developer API keys.
"""
import re
import json
import urllib.request
import urllib.parse
from typing import Tuple, List, Dict, Any, Optional

SPOTIFY_URL_REGEX = re.compile(
    r"(?:https?:\/\/)?(?:open\.)?spotify\.com(?:\/[a-zA-Z]{2}(?:-[a-zA-Z]{2})?)?\/(track|album|playlist|artist)[/:]([a-zA-Z0-9]+)|spotify:(track|album|playlist|artist):([a-zA-Z0-9]+)|(?:https?:\/\/)?spotify\.link\/([a-zA-Z0-9]+)",
    re.IGNORECASE
)

def is_spotify_url(query: str) -> bool:
    """Checks if a given string contains a Spotify URL or URI."""
    return bool(SPOTIFY_URL_REGEX.search(query))

def resolve_spotify(url: str) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """
    Extracts track or collection metadata from a Spotify link.
    Returns (entity_type, list_of_track_dicts).
    Each track dict contains:
      - 'title': str
      - 'artist': str
      - 'query': str (searchable for YouTube / SoundCloud)
      - 'duration_ms': int
      - 'thumbnail': Optional[str]
      - 'container': Optional[str] (album or playlist name)
    """
    clean = url.strip()
    
    # Follow redirect for spotify.link short links
    if "spotify.link" in clean:
        try:
            req = urllib.request.Request(clean, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                clean = resp.geturl()
        except Exception:
            pass

    # Strip query parameters (?si=...)
    clean = clean.split("?")[0].rstrip("/")

    # Match entity type and ID
    m = re.search(r"(track|album|playlist|artist)[/:]([a-zA-Z0-9]+)", clean)
    if not m:
        return None, []

    entity_type = m.group(1).lower()
    entity_id = m.group(2)

    # Primary method: Spotify Embed JSON state (__NEXT_DATA__)
    try:
        embed_url = f"https://open.spotify.com/embed/{entity_type}/{entity_id}"
        req = urllib.request.Request(
            embed_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
        if match:
            data = json.loads(match.group(1))
            entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
            if entity and entity.get("name"):
                results = []
                container_title = entity.get("name")

                if entity_type == "track":
                    title = entity.get("name")
                    artists = [a.get("name") for a in entity.get("artists", []) if a.get("name")]
                    artist_str = ", ".join(artists) if artists else "Unknown Artist"
                    search_query = f"{title} {artist_str}".strip()
                    thumb = None
                    try:
                        images = entity.get("visualIdentity", {}).get("image", [])
                        if images and isinstance(images, list):
                            thumb = images[0].get("url")
                    except Exception:
                        pass

                    results.append({
                        "title": title,
                        "artist": artist_str,
                        "query": search_query,
                        "duration_ms": entity.get("duration", 0),
                        "thumbnail": thumb,
                        "container": container_title,
                    })
                    return entity_type, results

                elif entity_type in ("album", "playlist"):
                    track_list = entity.get("trackList", [])
                    for t in track_list:
                        t_title = t.get("title")
                        t_subtitle = t.get("subtitle") or ""
                        if t_title:
                            results.append({
                                "title": t_title,
                                "artist": t_subtitle,
                                "query": f"{t_title} {t_subtitle}".strip(),
                                "duration_ms": t.get("duration", 0),
                                "thumbnail": None,
                                "container": container_title,
                            })
                    if results:
                        return entity_type, results
    except Exception as e:
        print(f"[SPOTIFY] Embed extraction error for '{url}': {e}", flush=True)

    # Fallback method: Spotify oEmbed API
    try:
        api_url = f"https://open.spotify.com/oembed?url={urllib.parse.quote(clean)}"
        req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
            title = data.get("title")
            if title:
                thumb = data.get("thumbnail_url")
                return "track", [{
                    "title": title,
                    "artist": "",
                    "query": title,
                    "duration_ms": 0,
                    "thumbnail": thumb,
                    "container": None,
                }]
    except Exception as e:
        print(f"[SPOTIFY] oEmbed fallback error for '{url}': {e}", flush=True)

    return None, []
