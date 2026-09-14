"""Streamable video upload client.

Uses Streamable's undocumented upload endpoint (``POST /upload``) with basic
auth.  The returned URL (``https://streamable.com/<shortcode>``) is
automatically expanded by Discord into a playable inline video embed — no
oEmbed or iframe hacking needed.

Environment variables:
    STREAMABLE_USERNAME  — Streamable account email
    STREAMABLE_PASSWORD  — Streamable account password

The credentials are optional: if either is missing, ``upload()`` returns
``None`` and the caller falls back to a direct file link.
"""
from __future__ import annotations

import base64
import os
import time
import urllib.error
import urllib.request
import json as _json
from dataclasses import dataclass

_USERNAME: str = (os.environ.get("STREAMABLE_USERNAME") or "").strip()
_PASSWORD: str = (os.environ.get("STREAMABLE_PASSWORD") or "").strip()

#: How long to poll for the video to finish processing (seconds).
POLL_TIMEOUT: int = 120
#: Seconds between polls.
POLL_INTERVAL: int = 3

#: API base.
_API = "https://api.streamable.com"


@dataclass(frozen=True)
class UploadResult:
    """A successful upload."""
    shortcode: str
    url: str
    thumbnail_url: str


def _auth_header() -> str:
    """Basic auth header value."""
    creds = f"{_USERNAME}:{_PASSWORD}"
    return "Basic " + base64.b64encode(creds.encode()).decode()


def _headers() -> dict[str, str]:
    return {
        "Authorization": _auth_header(),
    }


def available() -> bool:
    """True when credentials are configured."""
    return bool(_USERNAME and _PASSWORD)


def upload(path: str, title: str = "") -> UploadResult | None:
    """Upload a video file to Streamable and poll until it is ready.

    Returns an :class:`UploadResult` on success, or ``None`` if anything
    goes wrong (bad credentials, network error, timeout).
    """
    if not available():
        return None

    filename = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]

    try:
        shortcode = _upload_file(path, filename, title)
        if shortcode is None:
            return None
        _poll_until_ready(shortcode)
        return UploadResult(
            shortcode=shortcode,
            url=f"https://streamable.com/{shortcode}",
            thumbnail_url=f"https://thumbs-east.streamable.com/image/{shortcode}.jpg",
        )
    except Exception as exc:
        print(f"[STREAMABLE] Upload failed: {exc}", flush=True)
        return None


def _upload_file(path: str, filename: str, title: str) -> str | None:
    """Multipart POST to /upload. Returns the shortcode or None."""
    boundary = f"----FormBoundary{int(time.time() * 1000)}"

    # Build multipart body
    parts: list[bytes] = []

    # Title field
    if title:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="title"\r\n\r\n'
            f"{title}\r\n".encode()
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
        f"{_API}/upload",
        data=body,
        headers={
            "Authorization": _auth_header(),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = _json.loads(resp.read())
            return data.get("shortcode")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode()[:500]
        print(f"[STREAMABLE] HTTP {exc.code}: {body_text}", flush=True)
        return None
    except Exception as exc:
        print(f"[STREAMABLE] Request failed: {exc}", flush=True)
        return None


def _poll_until_ready(shortcode: str) -> None:
    """Poll GET /videos/<shortcode> until status == 2 (ready) or timeout."""
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                f"{_API}/videos/{shortcode}",
                headers=_headers(),
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read())
                status = data.get("status", 0)
                if status == 2:
                    return  # ready
                if data.get("message"):
                    print(f"[STREAMABLE] Processing: {data['message']}", flush=True)
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)
    print(f"[STREAMABLE] ⚠️ Polling timed out for {shortcode}", flush=True)
