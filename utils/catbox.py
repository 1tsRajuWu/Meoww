"""File hosting client using catbox.moe.

catbox.moe is an anonymous, permanent file host with a 200 MB per-file limit
and **no API key required**.  Unlike an "empty folder" link, the bytes are
uploaded immediately, so the URL we hand back already contains the file —
Discord renders images inline and plays mp4/webm links as an inline video.

The upload is streamed straight off disk, so a 100 MB attachment never has to
sit in memory on the bot host.

Environment variables:
    CATBOX_USER_AGENT — override the User-Agent (defaults to a bot UA)
"""
from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

#: Anonymous upload endpoint.
_API = "https://catbox.moe/user/api.php"

#: Anonymous per-file limit (200 MB).
MAX_SIZE: int = 200 * 1024 * 1024

#: Chunk size used while streaming the body off disk.
_CHUNK: int = 1024 * 1024

#: Cloudflare and catbox both reject requests without a sane User-Agent.
_USER_AGENT: str = os.environ.get(
    "CATBOX_USER_AGENT",
    "MeowDiscordBot/2.0 (+https://github.com/1tsRajOwO/meow-discord-bot)",
)

_UNSAFE = re.compile(r"[^A-Za-z0-9._ ()\[\]-]")


@dataclass(frozen=True)
class UploadResult:
    """A successful upload."""
    link: str
    size: int


def available() -> bool:
    """catbox needs no credentials, so it is always a candidate."""
    return True


def _safe_filename(filename: str) -> str:
    """Strip anything that could break the multipart headers."""
    name = _UNSAFE.sub("_", os.path.basename(filename)).strip() or "upload.bin"
    return name[:180]


def _content_type(filename: str) -> str:
    """A best-effort MIME type (catbox does not require accuracy)."""
    import mimetypes

    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _body_parts(path: str, filename: str, boundary: str, size: int):
    """Yield the multipart body, reading the file from disk in chunks.

    Returns the byte length of the whole body so the caller can set an exact
    ``Content-Length`` (``http.client`` would otherwise fall back to chunked
    transfer encoding, which catbox rejects).
    """
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="reqtype"\r\n\r\n'
        f"fileupload\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="fileToUpload"; '
        f'filename="{filename}"\r\n'
        f"Content-Type: {_content_type(filename)}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    length = len(head) + size + len(tail)

    def _iter():
        yield head
        with open(path, "rb") as fh:
            while True:
                block = fh.read(_CHUNK)
                if not block:
                    break
                yield block
        yield tail

    return _iter(), length


def upload(path: str, filename: str | None = None) -> UploadResult | None:
    """Upload a file to catbox.moe and return its permanent public URL.

    Returns ``None`` on any failure (network, size, rejected upload) so the
    caller can fall through to the next service.
    """
    filename = _safe_filename(filename or os.path.basename(path))
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        print(f"[CATBOX] Cannot stat {path}: {exc}", flush=True)
        return None

    if size <= 0:
        print("[CATBOX] Refusing to upload an empty file", flush=True)
        return None
    if size > MAX_SIZE:
        print(f"[CATBOX] File is {size} bytes — over the {MAX_SIZE} byte limit", flush=True)
        return None

    boundary = f"----MeowMeow{os.urandom(12).hex()}"
    body, length = _body_parts(path, filename, boundary, size)

    req = urllib.request.Request(
        _API,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(length),
            "User-Agent": _USER_AGENT,
            "Accept": "text/plain, */*",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            text = resp.read(2000).decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", "replace").strip()
        # catbox occasionally answers 5xx even though the file landed in its
        # store, and puts the resulting URL in the body. Don't throw away a
        # file the user already paid bandwidth for.
        if detail.startswith("http"):
            return UploadResult(link=detail.split()[0], size=size)
        print(f"[CATBOX] HTTP {exc.code}: {detail[:200]}", flush=True)
        return None
    except Exception as exc:
        print(f"[CATBOX] Upload failed: {exc}", flush=True)
        return None

    if not text.startswith("http"):
        print(f"[CATBOX] Unexpected response: {text[:200]}", flush=True)
        return None

    return UploadResult(link=text.split()[0], size=size)
