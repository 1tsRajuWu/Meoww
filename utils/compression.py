"""gzip for the answers this process writes — bandwidth, paid for in bytes.

The host bills for the bytes this process *sends*, and the pages it sends are
the kind that compresses best: a dashboard page is 60 KB of markup that gzips to
8 KB, the market's live board is 13.5 KB of JSON that gzips to 2.6 KB, and the
same browsers ask for them again and again.

Nothing downstream can be relied on to do it for us. A browser reaching this
process through the published site arrives via a proxy that forwards the request
without an ``Accept-Encoding`` of its own, so the origin answers uncompressed,
the bytes leave this host uncompressed, and that is the number on the invoice —
whatever the edge does with them afterwards. Compressing here makes the saving
independent of every hop in front of us: it is the same bytes on Render, on a
Space, or on a laptop.

Two rules keep this from being a way to make things worse:

* **Only what is worth it.** Text over :data:`MIN_BYTES`, and only for the
  types that actually shrink. A 500-byte greeting and a PNG are both left
  exactly as they are — gzip would add its own header to the first and nothing
  to the second.
* **Never twice.** An answer that already says what encoding it carries is
  somebody else's decision, re-encoded bodies would be unreadable, and a
  ``304`` has no body at all.

Registered before the security-header pass so it runs *after* it: that pass sets
``Vary`` for the two market endpoints, and appending to a header the other pass
just wrote is only correct in this order.
"""
from __future__ import annotations

import gzip

from flask import request

#: Below this many bytes the gzip envelope costs more than it saves.
MIN_BYTES: int = 500

#: 6 is the knee of the curve for the text this process writes: within a few
#: percent of the best ratio for a fraction of the CPU a request is waiting on.
LEVEL: int = 6

#: Encodings that mean "already done", or "somebody else's decision".
_ENCODINGS: tuple[str, ...] = ("gzip", "br", "deflate", "zstd", "compress")

#: ``text/*`` plus the two application types this app answers with.
_COMPRESSIBLE_EXACT: tuple[str, ...] = (
    "application/json",
    "application/javascript",
    "application/xml",
    "application/xhtml+xml",
    "application/x-ndjson",
    "application/yaml",
    "image/svg+xml",
)

#: Statuses that must not be touched: no body, or not an answer worth encoding.
_BODILESS_STATUSES: frozenset[int] = frozenset({204, 205, 304})


def accepted_encodings(header: str | None) -> dict[str, float]:
    """What a client says it takes, with its own ``q=0`` refusals honoured.

    ``Accept-Encoding: gzip;q=0`` is a client saying **no** in the only grammar
    that has one, and a parser that only looks for the token would send it gzip
    anyway.
    """
    accepted: dict[str, float] = {}
    for part in (header or "").split(","):
        piece = part.strip()
        if not piece:
            continue
        name, _, params = piece.partition(";")
        quality = 1.0
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
        accepted[name.strip().lower()] = quality
    return accepted


def accepts_gzip(header: str | None) -> bool:
    """Whether this ``Accept-Encoding`` header asks for gzip at all."""
    accepted = accepted_encodings(header)
    if accepted.get("gzip", 0.0) > 0:
        return True
    # A wildcard takes everything not named, and gzip is not named here.
    return "*" in accepted and accepted["*"] > 0 and "gzip" not in accepted


def is_compressible(content_type: str | None) -> bool:
    """Whether this content type is text that shrinks, rather than bytes."""
    mime = (content_type or "").split(";")[0].strip().lower()
    if not mime:
        return False
    if mime.startswith("text/"):
        return True
    return mime in _COMPRESSIBLE_EXACT


def skip_reason(response) -> str | None:
    """Why this answer must be left alone, or ``None`` when it may be gzipped.

    The reason is a sentence rather than a boolean so a test can hold the *rule*
    instead of a count: the point of each branch is which answer it protects.
    """
    if response.direct_passthrough:
        # A file on disk, already framed and usually already compressed.
        return "a body that is being streamed straight from a file"
    if response.is_streamed:
        return "a body that is still being produced"
    if response.status_code in _BODILESS_STATUSES:
        return "an answer that has no body"
    if response.status_code < 200:
        return "an answer that is not a result"
    if response.headers.get("Content-Encoding"):
        return "an answer that is already encoded"
    if not is_compressible(response.headers.get("Content-Type")):
        return "a body that is not text"
    if len(response.get_data()) < MIN_BYTES:
        return f"a body under {MIN_BYTES} bytes"
    return None


def compress(response) -> bool:
    """gzip this answer in place. ``False`` when it was left alone.

    The body is replaced rather than wrapped, so the ``Content-Length`` this
    response reports is the number of bytes that will actually be sent — a
    length that described the *uncompressed* body would be a lie a proxy is
    entitled to act on.
    """
    if skip_reason(response):
        return False
    try:
        packed = gzip.compress(response.get_data(), LEVEL)
    except Exception:
        # A body that cannot be read is not a reason to fail the request it
        # belongs to; it is a reason to send it as it is.
        return False
    response.set_data(packed)
    response.headers["Content-Encoding"] = "gzip"
    # Anything that varies by encoding varies, so a cache must key on it. An
    # existing ``Vary`` is added to rather than replaced: the market endpoints
    # already vary by ``Origin``, and dropping that would serve one site's
    # allowance to another's caller.
    varied = [v.strip() for v in response.headers.get("Vary", "").split(",") if v.strip()]
    if "Accept-Encoding" not in varied:
        varied.append("Accept-Encoding")
    response.headers["Vary"] = ", ".join(varied)
    return True


def register(app) -> None:
    """Turn compression on for every answer this app writes."""

    @app.after_request
    def _gzip_textual_answers(response):
        if not accepts_gzip(request.headers.get("Accept-Encoding")):
            return response
        compress(response)
        return response
