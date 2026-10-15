#!/usr/bin/env python3
"""
Research toolkit: search web, search YouTube, fetch content, summarize.
Uses: websearch (tool), webfetch (tool), yt-dlp (venv).
"""
import asyncio
import json
import sys
import os
import subprocess
import urllib.parse
from pathlib import Path
from typing import Optional

# Add project root to path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VENV_PYTHON = str(ROOT / ".venv" / "bin" / "python")


def yt_search(query: str, max_results: int = 5) -> list[dict]:
    """Search YouTube via yt-dlp, return list of video dicts."""
    # Escape query for Python string
    escaped_query = query.replace('"', '\\"').replace("'", "\\'")
    cmd = [
        VENV_PYTHON, "-c", f"""
import yt_dlp, json
ydl = yt_dlp.YoutubeDL({{
    'quiet': True,
    'skip_download': True,
    'extract_flat': 'in_playlist',
    'default_search': 'ytsearch{max_results}',
}})
result = ydl.extract_info('{escaped_query}', download=False)
entries = result.get('entries', []) if result else []
for e in entries:
    print(json.dumps({{
        'id': e.get('id'),
        'title': e.get('title'),
        'url': e.get('url') or f'https://youtu.be/{{e.get(\"id\")}}',
        'duration': e.get('duration'),
        'channel': e.get('channel') or e.get('uploader')
    }}))
"""
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        results = []
        for line in out.stdout.strip().split('\n'):
            if line.strip():
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return results
    except Exception as e:
        return [{"error": str(e)}]


def yt_video_info(video_url_or_id: str) -> dict:
    """Get full video info including description, chapters, transcripts if available."""
    escaped = video_url_or_id.replace('"', '\\"').replace("'", "\\'")
    cmd = [
        VENV_PYTHON, "-c", f"""
import yt_dlp, json
ydl = yt_dlp.YoutubeDL({{
    'quiet': True,
    'skip_download': True,
    'writesubtitles': True,
    'writeautomaticsub': True,
    'subtitleslangs': ['en', 'en-US', 'en-GB'],
}})
result = ydl.extract_info('{escaped}', download=False)
if result:
    print(json.dumps({{
        'id': result.get('id'),
        'title': result.get('title'),
        'url': result.get('webpage_url') or f'https://youtu.be/{{result.get("id")}}',
        'duration': result.get('duration'),
        'channel': result.get('channel') or result.get('uploader'),
        'channel_url': result.get('channel_url'),
        'upload_date': result.get('upload_date'),
        'view_count': result.get('view_count'),
        'like_count': result.get('like_count'),
        'description': result.get('description', '')[:3000],
        'tags': result.get('tags', [])[:20],
        'chapters': result.get('chapters'),
        'automatic_captions': list(result.get('automatic_captions', {{}}).keys()),
        'subtitles': list(result.get('subtitles', {{}}).keys()),
    }}))
"""
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if out.stdout.strip():
            return json.loads(out.stdout.strip())
        return {"error": out.stderr.strip() or "no output"}
    except Exception as e:
        return {"error": str(e)}


def yt_transcript(video_url_or_id: str, lang: str = "en") -> Optional[str]:
    """Try to fetch transcript/subtitles for a video."""
    escaped = video_url_or_id.replace('"', '\\"').replace("'", "\\'")
    cmd = [
        VENV_PYTHON, "-c", f"""
import yt_dlp
ydl = yt_dlp.YoutubeDL({{
    'quiet': True,
    'skip_download': True,
    'writesubtitles': True,
    'writeautomaticsub': True,
    'subtitleslangs': ['{lang}'],
    'subtitlesformat': 'vtt',
}})
result = ydl.extract_info('{escaped}', download=False)
if result:
    subs = result.get('subtitles', {{}}).get('{lang}', [])
    auto = result.get('automatic_captions', {{}}).get('{lang}', [])
    for s in subs + auto:
        if s.get('ext') == 'vtt' and s.get('url'):
            print(s['url'])
            break
"""
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        vtt_url = out.stdout.strip()
        if vtt_url:
            import urllib.request
            vtt = urllib.request.urlopen(vtt_url, timeout=10).read().decode('utf-8')
            # Simple VTT -> text
            lines = []
            for line in vtt.split('\n'):
                if line and not line.startswith('WEBVTT') and not line.startswith('NOTE') and '-->' not in line and not line.strip().isdigit():
                    lines.append(line)
            return '\n'.join(lines)[:10000]
    except Exception:
        pass
    return None


def fetch_url(url: str) -> dict:
    """Fetch a URL and return content. Uses urllib."""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; ResearchBot/1.0)'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode('utf-8', errors='replace')
            return {"url": url, "status": resp.status, "content": content[:15000], "content_type": resp.headers.get('Content-Type', '')}
    except Exception as e:
        return {"url": url, "error": str(e)}


def web_search(query: str, max_results: int = 10, engine: str = "duckduckgo") -> list[dict]:
    """
    Search the web via HTTP request to lite.duckduckgo.com (no JS, no blocking).
    Engines: duckduckgo (lite), brave, startpage
    """
    import urllib.parse as urlparse
    engines = {
        "duckduckgo": f"https://lite.duckduckgo.com/lite/?q={urlparse.quote(query)}",
        "duckduckgo-full": f"https://duckduckgo.com/html/?q={urlparse.quote(query)}&kl=us-en",
        "brave": f"https://search.brave.com/search?q={urlparse.quote(query)}&source=web",
        "startpage": f"https://www.startpage.com/sp/search?q={urlparse.quote(query)}",
    }
    url = engines.get(engine, engines["duckduckgo"])
    
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
            'Connection': 'keep-alive',
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='replace')
        return parse_search_html(html, engine, max_results)
    except Exception as e:
        return [{"error": str(e), "engine": engine}]


def parse_search_html(html: str, engine: str, max_results: int) -> list[dict]:
    """Parse search results from HTML."""
    import re
    from urllib.parse import unquote
    
    results = []
    
    if engine in ("duckduckgo", "duckduckgo-full"):
        # Lite version: <a rel="nofollow" href="..." class='result-link'>Title</a>
        # followed by <td class='result-snippet'>Snippet</td>
        # Both tags can span multiple lines, so use DOTALL flag
        link_pattern = re.compile(r"<a[^>]*href=['\"]([^'\"]+)['\"][^>]*class='result-link'[^>]*>([^<]+)</a>", re.DOTALL)
        snippet_pattern = re.compile(r"<td[^>]*class='result-snippet'[^>]*>([\s\S]*?)</td>", re.DOTALL)
        
        links = link_pattern.findall(html)
        snippets = snippet_pattern.findall(html)
        
        for i, (raw_url, title) in enumerate(links[:max_results]):
            # Decode DDG redirect URL
            clean_url = raw_url
            if "uddg=" in raw_url:
                m = re.search(r"uddg=([^&]+)", raw_url)
                if m:
                    clean_url = unquote(m.group(1))
            
            snippet = snippets[i] if i < len(snippets) else ""
            results.append({
                "title": title.strip(),
                "url": clean_url,
                "snippet": snippet.strip(),
                "engine": engine
            })
    
    elif engine == "brave":
        pass
    
    elif engine == "startpage":
        pass
    
    return results


# ---- Main CLI ----
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Research toolkit")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_search = sub.add_parser("yt-search", help="Search YouTube")
    p_search.add_argument("query")
    p_search.add_argument("-n", "--max", type=int, default=5)

    p_info = sub.add_parser("yt-info", help="Get video details")
    p_info.add_argument("video")

    p_transcript = sub.add_parser("yt-transcript", help="Get transcript")
    p_transcript.add_argument("video")
    p_transcript.add_argument("-l", "--lang", default="en")

    p_fetch = sub.add_parser("fetch", help="Fetch a URL")
    p_fetch.add_argument("url")

    p_web = sub.add_parser("web-search", help="Search the web via browser")
    p_web.add_argument("query")
    p_web.add_argument("-n", "--max", type=int, default=10)
    p_web.add_argument("-e", "--engine", choices=["duckduckgo", "google", "bing", "brave"], default="duckduckgo")

    args = parser.parse_args()

    if args.cmd == "yt-search":
        results = yt_search(args.query, args.max)
        print(json.dumps(results, indent=2))
    elif args.cmd == "yt-info":
        info = yt_video_info(args.video)
        print(json.dumps(info, indent=2))
    elif args.cmd == "yt-transcript":
        text = yt_transcript(args.video, args.lang)
        print(text or "No transcript available")
    elif args.cmd == "fetch":
        result = fetch_url(args.url)
        print(json.dumps(result, indent=2))
    elif args.cmd == "web-search":
        results = web_search(args.query, args.max, args.engine)
        print(json.dumps(results, indent=2))