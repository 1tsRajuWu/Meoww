"""The picture somebody attached, read into the answer without touching the disk.

Discord hands the bot an attachment URL on its own CDN. This module fetches it and
base64-encodes it in memory, which is what Ollama's ``images`` field wants — so
the file is **never written anywhere**. That is one fewer copy of something
somebody chose to send, and it is the reason there is no temp-directory cleanup
to get wrong.

The extra care here is about *sizes*: an attachment can be 25 MB of raw camera
output, and a vision model that is handed that answers slowly or not at all. The
cap is on the encoded size, the failure is a sentence rather than an exception,
and the question still gets answered without the picture — because "your image was
too big" is a better answer than nothing.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

from decat.mac import net

#: Discord serves attachments from these; nothing else is ever asked for a file.
CDN_HOSTS: tuple[str, ...] = (
    "cdn.discordapp.com",
    "media.discordapp.net",
    "images-ext-1.discordapp.net",
    "images-ext-2.discordapp.net",
)

#: Ten megabytes of image is roughly what a vision model will actually look at.
MAX_BYTES = 10 * 1024 * 1024


@dataclass
class Picture:
    """An attachment, encoded, or the reason it is not."""

    data: str = ""
    error: str = ""
    content_type: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.data) and not self.error


def fetch(url: str, *, timeout: float = 30.0, limit: int = MAX_BYTES) -> Picture:
    """Fetch an attachment and return it base64-encoded, or say why not."""
    if not url:
        return Picture(error="")
    try:
        raw, headers = net.request_bytes(url, allowed=CDN_HOSTS, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — a lost picture is not a lost question
        return Picture(error=f"could not fetch the image ({type(exc).__name__})")
    if len(raw) > limit:
        return Picture(error=f"the image is {len(raw) // 1024 // 1024} MB, which is too large to read")
    if not raw:
        return Picture(error="the image came back empty")
    return Picture(
        data=base64.b64encode(raw).decode("ascii"),
        content_type=str(headers.get("Content-Type", "")),
    )
