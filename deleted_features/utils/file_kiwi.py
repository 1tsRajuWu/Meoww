"""File hosting client using file.kiwi.

file.kiwi provides free file hosting with no file size limit and E2E encryption.
This module uses the "empty folder" approach:

1. Create a folder via the API
2. Return the folder URL to the user
3. User opens the link in a browser and uploads directly

This is ideal for large files because:
- No file size limit (up to 999 GiB per file)
- Files are E2E encrypted
- Links expire after 24 hours if no upload occurs
- After upload, files are available for 24 hours (free tier)

Environment variables:
    FILE_KIWI_MODE — Folder mode: "send" (default), "share", or "request"
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request
import json as _json
from dataclasses import dataclass

_API = "https://api.file.kiwi/v2/folders"

#: Default folder mode.
DEFAULT_MODE: str = os.environ.get("FILE_KIWI_MODE", "send")


@dataclass(frozen=True)
class FolderResult:
    """A successful folder creation."""
    folder_id: str
    folder_url: str
    mode: str


def create_folder(title: str = "File upload", mode: str | None = None) -> FolderResult | None:
    """Create an empty folder on file.kiwi and return the shareable URL.

    The user opens the URL in a browser and uploads files directly.
    If no file is uploaded within 24 hours, the folder is deleted.

    Returns a FolderResult on success, or None if anything goes wrong.
    """
    mode = mode or DEFAULT_MODE

    payload = _json.dumps({
        "title": title[:200],  # API limit: 200 chars
        "mode": mode,
        "encryption": {
            "keyStrategy": "server_generated"
        }
    }).encode("utf-8")

    req = urllib.request.Request(
        _API,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "MeowDiscordBot/2.0",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read())
            folder_id = data.get("folderId")
            folder_url = data.get("folderUrl")
            result_mode = data.get("mode", mode)

            if folder_id and folder_url:
                return FolderResult(
                    folder_id=folder_id,
                    folder_url=folder_url,
                    mode=result_mode,
                )
            print(f"[FILE.KIWI] No folder data in response: {data}", flush=True)
            return None
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode()[:500]
        print(f"[FILE.KIWI] HTTP {exc.code}: {body_text}", flush=True)
        return None
    except Exception as exc:
        print(f"[FILE.KIWI] Request failed: {exc}", flush=True)
        return None


def format_size(size_bytes: int) -> str:
    """Human-readable file size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
