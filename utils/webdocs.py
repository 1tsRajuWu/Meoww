"""The policies, as web pages.

``cogs/legal.py`` renders the same two documents into a Discord card, where the
budget is 6000 characters and a section that does not fit falls off silently. A
browser has no such budget, and a member who wants to read the policy they are
agreeing to should be able to do it on a real page — with the whole document,
its headings, and links they can actually follow.

So this is a deliberately small markdown renderer: the subset the two documents
in this repository actually use, and nothing else. It is **not** a markdown
implementation and it does not try to be one. Two rules it does keep:

* **Everything is escaped first.** The input is our own file, but a renderer that
  trusts its input is how a policy page becomes an injection point the day
  somebody pastes a snippet into the policy. Text is HTML-escaped before any
  markup is applied, so no tag in the source can survive as a tag.
* **Only ``http``/``https`` links become links.** ``[click](javascript:…)``
  renders as text, which is the correct answer for a document nobody reviews
  character by character.
* **A block is joined before it is rendered.** The documents wrap at 80 columns,
  so a bullet's sentence — and the emphasis or link inside it — spans newlines as
  a matter of course.

The page itself links ``cogs/legal.read_document`` for reading the file, so the
web page and the Discord card can never disagree about which file is the policy.
"""
import html
import re

#: ``[text](url)`` — the URL is checked before it is ever put in an href.
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_CODE = re.compile(r"`([^`]+)`")
_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


def _safe_url(url: str) -> str | None:
    """A URL we are willing to put in an ``href``, or ``None``."""
    candidate = url.strip()
    lowered = candidate.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return candidate
    return None


def _inline(text: str) -> str:
    """Escaped text with the inline markup the documents use applied."""
    out = html.escape(text, quote=False)

    def link(match: "re.Match[str]") -> str:
        label, url = match.group(1), html.unescape(match.group(2))
        target = _safe_url(url)
        if target is None:
            # Not a link we will make clickable — shown as what it says, with the
            # URL visible so the reader still knows what was written.
            return f"{label} ({html.escape(url, quote=True)})"
        return (
            f'<a href="{html.escape(target, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer">{label}</a>'
        )

    out = _CODE.sub(r"<code>\1</code>", out)
    out = _LINK.sub(link, out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _ITALIC.sub(r"<em>\1</em>", out)
    return out


def render(markdown: str) -> str:
    """The document as HTML: headings, paragraphs, lists, rules and links.

    Blank lines separate blocks, which is all the structure the two documents
    rely on. Anything unrecognised is a paragraph, so no line can be lost — a
    policy that silently drops a sentence is worse than one that renders plainly.
    """
    if not markdown:
        return ""

    blocks: list[str] = []
    paragraph: list[str] = []
    items: list[str] = []
    ordered = False

    def flush_paragraph() -> None:
        if paragraph:
            # Joined *first*, then rendered: the source wraps at 80 columns, so an
            # emphasis or a link routinely spans a newline and a per-line pass
            # cannot see it. PRIVACY.md has exactly one such span, and it printed
            # its asterisks on the page.
            blocks.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_list() -> None:
        if items:
            tag = "ol" if ordered else "ul"
            body = "".join(f"<li>{_inline(item)}</li>" for item in items)
            blocks.append(f"<{tag}>{body}</{tag}>")
            items.clear()

    for raw in markdown.splitlines():
        line = raw.rstrip()

        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            flush_list()
            # The document's own `#` line is the page's `<h1>`, which the template
            # renders — emitting it here as well would print the title twice.
            if len(heading.group(1)) == 1:
                continue
            level = min(len(heading.group(1)), 6)
            blocks.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue

        if _RULE.match(line):
            flush_paragraph()
            flush_list()
            blocks.append("<hr>")
            continue

        bullet = _BULLET.match(line)
        numbered = None if bullet else _NUMBERED.match(line)
        if bullet or numbered:
            flush_paragraph()
            match = bullet or numbered
            assert match is not None
            is_ordered = numbered is not None
            if items and is_ordered != ordered:
                flush_list()
            ordered = is_ordered
            items.append(match.group(1))
            continue

        if not line.strip():
            flush_paragraph()
            flush_list()
            continue

        if items:
            # The source documents wrap at 80 columns, so a bullet's sentence
            # routinely continues on the following line. Treating that line as a
            # new paragraph tore the sentence out of its bullet and printed the
            # second half as body text — which is exactly what the Privacy page
            # did before this branch existed ("… message IDs (a message" as one
            # bullet, "ID only where a feature anchors to one)." as a paragraph).
            items[-1] = f"{items[-1]} {line.strip()}"
            continue

        paragraph.append(line.strip())

    flush_paragraph()
    flush_list()
    return "\n".join(blocks)


def title_from(markdown: str, fallback: str) -> str:
    """The document's own ``# `` heading, for the page title."""
    for line in (markdown or "").splitlines():
        heading = _HEADING.match(line)
        if heading and len(heading.group(1)) == 1:
            return heading.group(2).strip()
    return fallback
