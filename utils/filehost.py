"""File hosting client using file.io.

file.io provides a simple API: POST a file, get a one-click download link.
Links expire after 14 days by default but can be set to never expire.

Used as a fallback when Streamable is not available or the file is not a
video (images, documents, archives, etc.).
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request
import json as _json
from dataclasses import dataclass

#: Default expiry: 14 days (file.io default).
#: Set to ``0`` for no expiry.
_EXPIRY_DAYS: int = int(os.environ.get("FILEIO_EXPIRY_DAYS", "14"))

_API = "https://file.io"


@dataclass(frozen=True)
class UploadResult:
    """A successful upload."""
    link: str
    size: int


def upload(path: str) -> UploadResult | None:
    """Upload a file to file.io and return the download link.

    Returns ``None`` on any error.
    """
    filename = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    file_size = os.path.getsize(path)

    boundary = f"----FormBoundary{int(__import__('time').time() * 1000)}"

    parts: list[bytes] = []

    # Expiry field
    if _EXPIRY_DAYS > 0:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="expires"\r\n\r\n'
            f"{_EXPIRY_DAYS}d\r\n".encode()
        )

    # File field
    with open(path, "rb") as f:
        file_data = f.read()
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
        + file_data
        + b"\r\n"
    )

    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        _API,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = _json.loads(resp.read())
            link = data.get("link")
            if link:
                return UploadResult(link=link, size=file_size)
            print(f"[FILE.IO] No link in response: {data}", flush=True)
            return None
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode()[:500]
        print(f"[FILE.IO] HTTP {exc.code}: {body_text}", flush=True)
        return None
    except Exception as exc:
        print(f"[FILE.IO] Upload failed: {exc}", flush=True)
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
