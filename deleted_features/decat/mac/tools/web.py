"""Reading the live web: search, fetch, and turn a page into something quotable.

Three decisions are load-bearing here, and each of them is a defence rather than
a feature.

**The model never asks for a page.** There is no tool loop: this module searches,
picks, fetches and extracts *deterministically*, and the model is handed the
result as quoted data. A model that can request URLs is a model that can be
talked into requesting one by the page it just read — and the page just read is
written by somebody who does not have the operator's interests at heart. The
extracted text is labelled as data in the prompt for the same reason.

**A page is fetched only from a host a search returned.** ``net.checked_endpoint``
wants an allow-list, and for the open web there is no honest static one. The
allow-list is therefore built per question, from the hosts the search actually
returned — so an injected instruction inside one page cannot make this code
fetch a host no search ever mentioned.

**A page that cannot be read says so.** The alternative is a model filling the
silence in from memory and the card citing the page anyway, which is the exact
failure the whole feature exists to avoid: :func:`gather` returns the sources it
*read*, and the caller marks the answer ungrounded when that list is empty.
"""
from __future__ import annotations

import html
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser

from decat.mac import net

#: Where a *search* may be sent. The pages themselves are checked against the
#: hosts the search returned, per :func:`gather` — see this module's docstring.
SEARCH_HOSTS: tuple[str, ...] = (
    "duckduckgo.com",
    "search.brave.com",
    "api.search.brave.com",
)

#: Elements whose text is never part of the page's meaning.
_SKIP = {"script", "style", "noscript", "svg", "template", "nav", "footer", "form"}
#: Elements that end a line of prose rather than running into the next.
_BREAK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
_QUERY_NOISE = re.compile(r"^\s*(hey|hi|hello|pls|please|can you|could you|tell me|i want to know)\b[,\s]*", re.I)


@dataclass
class Page:
    """One page, as far as it can be read."""

    url: str
    title: str = ""
    text: str = ""

    def excerpt(self, limit: int = 1200) -> str:
        body = re.sub(r"\s+", " ", self.text).strip()
        return body[:limit]


@dataclass
class WebResult:
    """What one question's worth of searching produced."""

    pages: list[Page] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    provider: str = "none"

    @property
    def grounded(self) -> bool:
        return bool(self.pages)

    def sources(self) -> tuple[tuple[str, str], ...]:
        """``(title, url)`` per page read, in the order the model will cite them."""
        return tuple(
            (page.title or urllib.parse.urlsplit(page.url).hostname or page.url, page.url)
            for page in self.pages
        )


class _Text(HTMLParser):
    """The smallest reader that gets prose out of a page.

    Deliberately not a full parser and deliberately not a dependency: the laptop
    should be able to run this half with nothing but the standard library, and
    what is needed here is the text a reader would see, not the tree.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        text = data.strip()
        if not text or self._skip:
            return
        if self._in_title:
            self.title = (self.title + " " + text).strip()
            return
        self.parts.append(text + " ")

    def text(self) -> str:
        joined = "".join(self.parts)
        return re.sub(r"\n{2,}", "\n", re.sub(r"[ \t]{2,}", " ", joined)).strip()


def readable(page_html: str, *, limit: int = 20000) -> tuple[str, str]:
    """``(title, text)`` for a page's HTML, capped and whitespace-normalised."""
    parser = _Text()
    try:
        parser.feed(page_html[:limit])
        parser.close()
    except Exception:  # noqa: BLE001 — malformed markup is not a reason to fail a question
        pass
    return html.unescape(parser.title), parser.text()


def queries_for(question: str, *, limit: int = 2) -> list[str]:
    """The queries worth running for a question — the question, mostly.

    Refinement is deliberately *not* a model call: it costs a round trip before a
    second of searching has happened, and on a laptop the difference between "the
    question" and "a model's paraphrase of the question" is not the difference
    between finding the answer and not. Stripping conversational filler is.
    """
    text = re.sub(r"\s+", " ", str(question or "")).strip()
    # Stripped in a loop, because the filler stacks: "hey, can you please tell me"
    # is three of these, and one pass leaves the other two in the query.
    stripped = text
    while True:
        shorter = _QUERY_NOISE.sub("", stripped).strip()
        if shorter == stripped or not shorter:
            break
        stripped = shorter
    stripped = stripped or text
    if len(stripped) <= 120:
        return [stripped][:limit]
    # A long question is searched in halves: the first sentence is usually the
    # ask, and the rest is usually the context that would bury it.
    head = re.split(r"(?<=[.?!])\s+", stripped)[0].strip()
    out = [head or stripped[:120]]
    if len(out) < limit:
        tail = stripped[len(head) :].strip()[:120]
        if tail:
            out.append(tail)
    return out[:limit]


# ── search ──────────────────────────────────────────────────────────────────

def _ddg_url(query: str) -> str:
    return "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)


_RESULT_LINK = re.compile(
    r'<a[^>]+class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.I | re.S,
)


def _unescape_title(raw: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()


def _unwrap_ddg(href: str) -> str:
    """DuckDuckGo wraps results in a redirect; the real URL is the ``uddg``."""
    href = html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlsplit(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        real = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
        if real:
            return urllib.parse.unquote(real)
    return href


def parse_ddg(page_html: str, *, limit: int = 5) -> list[tuple[str, str]]:
    """``(title, url)`` for a DuckDuckGo HTML results page."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _RESULT_LINK.finditer(page_html or ""):
        url = _unwrap_ddg(match.group("href"))
        if not url.startswith("https://") or url in seen:
            continue
        seen.add(url)
        found.append((_unescape_title(match.group("title")), url))
        if len(found) >= limit:
            break
    return found


def parse_json_results(payload: object, *, limit: int = 5) -> list[tuple[str, str]]:
    """``(title, url)`` from Brave's or SearXNG's answer, whichever arrived."""
    entries: list = []
    if isinstance(payload, dict):
        if isinstance(payload.get("web"), dict):
            entries = list(payload["web"].get("results") or [])
        elif isinstance(payload.get("results"), list):
            entries = list(payload["results"])
    found: list[tuple[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url.startswith("https://"):
            continue
        found.append((str(entry.get("title") or url), url))
        if len(found) >= limit:
            break
    return found


def search(
    query: str,
    *,
    provider: str,
    api_key: str = "",
    searxng_url: str = "",
    limit: int = 5,
    timeout: float = 20.0,
) -> list[tuple[str, str]]:
    """Ask the configured engine. Raises ``OSError`` when it cannot be asked."""
    if provider in {"", "none"}:
        raise OSError("no search provider is configured")
    if provider == "ddg":
        page = net.get_text(_ddg_url(query), allowed=SEARCH_HOSTS, timeout=timeout)
        return parse_ddg(page, limit=limit)
    if provider == "brave":
        if not api_key:
            raise OSError("brave needs DECAT_SEARCH_KEY")
        payload = net.get_json(
            "https://api.search.brave.com/res/v1/web/search?count=%d&q=%s"
            % (limit, urllib.parse.quote_plus(query)),
            allowed=SEARCH_HOSTS,
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
            timeout=timeout,
        )
        return parse_json_results(payload, limit=limit)
    if provider == "searxng":
        if not searxng_url:
            raise OSError("searxng needs DECAT_SEARXNG_URL")
        host = urllib.parse.urlsplit(searxng_url).hostname or ""
        payload = net.get_json(
            f"{searxng_url}/search?format=json&q={urllib.parse.quote_plus(query)}",
            allowed=(host,),
            timeout=timeout,
        )
        return parse_json_results(payload, limit=limit)
    raise OSError(f"unknown search provider {provider!r}")


# ── the whole job ───────────────────────────────────────────────────────────

def fetch_pages(
    results: list[tuple[str, str]],
    *,
    limit: int = 3,
    timeout: float = 25.0,
) -> tuple[list[Page], list[str]]:
    """Read the first few results, from the hosts the search itself returned.

    The allow-list is *these* hosts and no others — see the module docstring. A
    page that fails is recorded by host and reason, never by content.
    """
    hosts = tuple(
        sorted({
            urllib.parse.urlsplit(url).hostname or ""
            for _title, url in results
            if urllib.parse.urlsplit(url).hostname
        })
    )
    pages: list[Page] = []
    failures: list[str] = []
    for title, url in results[:limit]:
        try:
            markup = net.get_text(url, allowed=hosts, timeout=timeout, limit=900_000)
        except Exception as exc:  # noqa: BLE001 — one unreadable page is not a failed question
            failures.append(f"{urllib.parse.urlsplit(url).hostname}: {type(exc).__name__}")
            continue
        read_title, text = readable(markup)
        pages.append(Page(url=url, title=read_title or title, text=text))
    return pages, failures


def gather(
    question: str,
    *,
    provider: str = "ddg",
    api_key: str = "",
    searxng_url: str = "",
    pages: int = 3,
    queries: int = 2,
) -> WebResult:
    """Search, read, and report — the one call the agent makes."""
    result = WebResult(provider=provider)
    if provider in {"", "none"}:
        return result
    seen: set[str] = set()
    for query in queries_for(question, limit=queries):
        result.queries.append(query)
        try:
            found = search(query, provider=provider, api_key=api_key, searxng_url=searxng_url)
        except Exception as exc:  # noqa: BLE001 — recorded, not raised: the answer still exists
            result.failures.append(f"search: {type(exc).__name__}")
            continue
        fresh = [(title, url) for title, url in found if url not in seen]
        seen.update(url for _title, url in fresh)
        if not fresh:
            continue
        read, failures = fetch_pages(fresh, limit=max(0, pages - len(result.pages)))
        result.pages.extend(read)
        result.failures.extend(failures)
        if len(result.pages) >= pages:
            break
    return result


def sources_block(result: WebResult) -> str:
    """The numbered sources, as the prompt and the card both see them."""
    lines = []
    for index, page in enumerate(result.pages, start=1):
        lines.append(f"[{index}] {page.title or page.url}\n{page.url}\n{page.excerpt()}")
    return "\n\n".join(lines)


__all__ = [
    "Page",
    "WebResult",
    "SEARCH_HOSTS",
    "gather",
    "parse_ddg",
    "parse_json_results",
    "queries_for",
    "readable",
    "search",
    "sources_block",
]
