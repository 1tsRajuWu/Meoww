#!/usr/bin/env python3
"""Render the bot's cards side by side: one revision against another.

A Components V2 card can only be seen in Discord, which makes every typographic
decision in ``utils/style.py`` unverifiable without sending one — and the cards
are the product's entire visual surface. This renders them, locally, as HTML.

Two things make it evidence rather than an artist's impression:

* **It renders what the builder actually produced.** Every card is built by
  calling the real module and read back from ``to_components()`` — the same JSON
  Discord receives — so the page cannot disagree with the code. A preview drawn
  from a hand-written mock would agree with whatever the author believed.
* **The "before" column is a real revision.** ``--before-rev`` is loaded out of
  git and built through the same fixture list, so the difference on the page is
  the difference in the tree. Nothing is copied into this file, which is the rule
  the rest of the repository lives by: a second copy of the builder would go stale
  the first time somebody edited the first one.

Usage (from the project root)::

    # an uncommitted pass: the working tree against the commit it sits on
    python scripts/preview_cards.py --before-rev HEAD

    # a committed pass: HEAD is the new one, HEAD~1 the old
    python scripts/preview_cards.py --before-rev HEAD~1

    # one card, for a change that touches a single shape
    python scripts/preview_cards.py --before-rev HEAD~1 --only action

Writes ``_preview/cards.html`` (git-ignored) and prints the path.
"""
from __future__ import annotations

import argparse
import html
import importlib.util
import os
import re
import subprocess
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402  (after the path fix, on purpose)

from utils import style  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT = os.path.join(ROOT, "_preview", "cards.html")

#: Discord's own component type numbers, from its component reference. Named with
#: a ``TYPE`` suffix because the page templates below are also module constants,
#: and a shared name silently turns every section in a card into an empty string —
#: which is exactly what happened to the first version of this file.
CONTAINER_TYPE, SECTION_TYPE, TEXT_TYPE, THUMBNAIL_TYPE = 17, 9, 10, 11
GALLERY_TYPE, FILE_TYPE, SEPARATOR_TYPE, ROW_TYPE = 12, 13, 14, 1


# --------------------------------------------------------------------------- #
# Loading a revision of the module
# --------------------------------------------------------------------------- #
def load_revision(rev: str, label: str) -> types.ModuleType:
    """The ``utils/style.py`` of ``rev``, imported under its own name."""
    source = subprocess.run(
        ["git", "show", f"{rev}:utils/style.py"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    module = types.ModuleType(f"meow_style_{label}")
    module.__file__ = f"<git:{rev}:utils/style.py>"
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


# --------------------------------------------------------------------------- #
# The fixtures — the same list is built through both revisions
# --------------------------------------------------------------------------- #
def _action(mod):
    """A roleplay card: the shape every /action, /rizz and /roast sends."""
    return mod.card(
        title="BOOP / POKE!",
        body="@rek_ter poked @unknown-user's squishy cheek repeatedly! *poke poke* 🫵",
        accent=0xE91E63,
        gallery_urls=["https://cataas.com/cat/gif"],
        footer="rek_ter → unknown-user",
    )


def _leaderboard(mod):
    """A card of fields: the worst case for hierarchy, and the common one."""
    return mod.card(
        title="🏆 Top Earners",
        body="The five richest members this week, by net worth.",
        fields=[
            ("1 · alice", "12,400 🪙"),
            ("2 · rek_ter", "9,150 🪙"),
            ("3 · bob", "6,020 🪙"),
            ("4 · carol", "3,480 🪙"),
            ("5 · dave", "980 🪙"),
        ],
        footer="Hmm · week to 21 Sep · `/balance` for your own",
    )


def _balance(mod):
    """Two fields and a footer: the smallest card that had the label problem."""
    return mod.card(
        title="💰 Wallet",
        body="Your balance right now.",
        fields=[("Wallet", "1,240 🪙"), ("Bank", "8,600 🪙"), ("Net worth", "9,840 🪙")],
        accent=mod.SUCCESS,
        footer="Meow Bank holds 3,100 🪙 of fees from this server",
    )


def _help_category(mod):
    """The help panel's body: a description of `>` command lines and a footer."""
    return mod.card(
        title="🛡️ Auto-Moderation",
        body=(
            "Automatic server defenses against spam and unauthorized invites.\n\n"
            "> `/automod invites <enabled>` — Toggle blocking of Discord invite links\n"
            "> `/automod antispam <enabled>` — Toggle rapid-message flood protection"
        ),
        accent=0xE67E22,
        footer="Browse a category anytime · Meow",
    )


def _alert(mod):
    """An operator alert: three named blocks and a count line."""
    return mod.card(
        title="🖼️ Some of the bot's media is dead",
        body=(
            "**2** of 23 media targets are dead. Every one of them is drawn as an "
            "empty box or a silent card, and nothing in Discord says why."
        ),
        fields=[
            ("Dead", "`cogs/social.py:293` — HTTP 404\nhttps://media.tenor.com/dead/wedding.gif"),
            ("This pass", "23 targets checked · 21 fine · 2 dead · 0 not reachable"),
            ("What to do", "A hardcoded link is the one to change: put it through `utils/gifs`."),
        ],
        accent=mod.DANGER,
        footer="Meow · media watch · weekly",
    )


def _ticket(mod):
    """A card with a control strip under it."""
    view = mod.card(
        title="🎫 Ticket claimed",
        body="@rek_ter is handling this request. A transcript is saved when it closes.",
        fields=[("Opened by", "alice"), ("Channel", "#ticket-0042")],
        accent=0xF1C40F,
        footer="Thanks for reaching out",
        timeout=None,
    )
    view.add_row(discord.ui.Button(label="Close", style=discord.ButtonStyle.secondary))
    view.add_row(discord.ui.Button(label="Transcript", style=discord.ButtonStyle.primary))
    return view


def _soundboard(mod):
    """A card that carries a file, which only exists when a component names it."""
    attachment = discord.File("assets/sounds/purr.ogg", filename="purr.ogg")
    return mod.card(
        title="🔊 Meeee-ow!",
        body="Classic Crisp Meow",
        fields=[("Sound", "🐱 Real Cat Audio"), ("", "Click the player above to listen!")],
        accent=0x1DB954,
        footer="Played for rek_ter · 2026-09-20 22:16 UTC",
        attachments=[attachment],
        timeout=None,
    )


def _messy(mod):
    """The hygiene case: a body that was written, not laid out.

    A hand-drawn divider, a run of blank lines, trailing spaces and a footer —
    every input ``clean_text`` exists for, in one card.
    """
    return mod.card(
        title="🧾 Receipt",
        body="Three channels posted to.   \n\n━━━━━━━━━━━\n\n\n\n#general · #clips · #memes\n",
        fields=[("Sent by", "rek_ter"), ("When", "21 Sep 03:41")],
        accent=0x3498DB,
        footer="The message is visible in the channels it landed in\n",
    )


def _caption(mod):
    """A card that is only a caption — a shape routes send when there is no body.

    This is where an orphan rule shows up: the footer's hairline used to be the
    card's *first* component, so the card opened with a rule and nothing above it.
    """
    return mod.card(title="", body="", footer="Sent · `/help` for everything else")


def _merged(mod):
    """Two embeds merged into one card, which is where the rule used to split them.

    ``embeds_to_layout`` appends the second embed's fields to an already-built
    card — *after* the footer and its rule — so the hairline ended up in the middle
    of the card with the closing line above the new content.
    """
    first = discord.Embed(title="🩺 Server health", description="All three readings answered.")
    first.set_footer(text="Meow · uptime · weekly")
    second = discord.Embed(
        title="Monitors",
        description="API 200 · Dashboard 200 · Public site 200",
    )
    return mod.embeds_to_layout([first, second])


def _converted_embed(mod):
    """An embed on its way to a card: author, title, fields and a timestamp."""
    embed = discord.Embed(
        title="Rank Up",
        description="You reached **level 5** in this server.",
        color=0x57F287,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name="Meow · Level System", icon_url="https://meow.test/icon.png")
    embed.add_field(name="XP", value="1,240 / 2,000")
    embed.add_field(name="Reward", value="120 🪙 and a new role")
    embed.set_footer(text="Hmm · levels")
    return mod.embed_to_layout(embed)


def _one_liner(mod):
    return mod.success("Sound posted to #general.", title="📤 Sent")


#: ``(key, what to look at, builder)``. The builders take the module, so the same
#: list builds a card in either revision — and only calls API that both have.
FIXTURES: tuple[tuple[str, str, callable], ...] = (
    ("action", "Hierarchy: the title is the subject, the joke is the body, the picture is a "
               "block rather than a gap, and the rule closes the card.", _action),
    ("leaderboard", "Fields: each name is a quiet label over its value, so the list reads "
                    "down for who and across for how much.", _leaderboard),
    ("balance", "Spacing: three facts grouped by air, not by rules, and one rule before the "
                "footer.", _balance),
    ("help", "A body of `>` command lines: no drawn divider, no double gap, and the accent "
             "comes from the palette rather than from a category's own hue.", _help_category),
    ("alert", "Named blocks: the alert's three sections are labels over bodies instead of "
              "three bold headings shouting the same volume.", _alert),
    ("ticket", "The control strip stays below the footer, so the rule separates content from "
               "the buttons rather than splitting them.", _ticket),
    ("soundboard", "A file component, referenced so Discord draws it, with the label/value "
                   "pair above it.", _soundboard),
    ("messy", "Text hygiene: the divider, the blank run and the trailing spaces are removed "
              "before the card is assembled.", _messy),
    ("caption", "An orphan rule: a card whose only content is its closing line used to "
                "open with the hairline that closes it.", _caption),
    ("merged", "Two embeds in one card: the second one's fields used to land under the "
               "footer and its rule, leaving the rule separating the close from them.", _merged),
    ("embed", "The embed conversion: the author becomes an eyebrow, the colour goes through "
              "`accent_for`, and the fields become labels.", _converted_embed),
    ("one_liner", "One line of feedback: still a card, with one accent and no rule at all.",
     _one_liner),
)


# --------------------------------------------------------------------------- #
# Rendering: the component JSON Discord would receive, as HTML
# --------------------------------------------------------------------------- #
def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"&lt;@!?(\d+)&gt;", r'<span class="mention">@\1</span>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<span class="link">\1</span>', text)
    return text


def _markdown(text: str) -> str:
    out = []
    for line in text.split("\n"):
        if line.startswith("### "):
            out.append(f"<h3>{_inline(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.startswith("-# "):
            out.append(f'<p class="small">{_inline(line[3:])}</p>')
        elif line.startswith("> "):
            out.append(f'<p class="quote">{_inline(line[2:])}</p>')
        elif not line.strip():
            out.append('<div class="blank"></div>')
        else:
            out.append(f"<p>{_inline(line)}</p>")
    return "".join(out)


def _media_frame(url: str, host_only: bool = False) -> str:
    host = url.split("//", 1)[-1].split("/", 1)[0]
    if host_only or not url:
        return f'<div class="media media--frame"><span>{html.escape(host or "attachment")}</span></div>'
    return (
        f'<div class="media"><img src="{html.escape(url)}" alt="" '
        f'onerror="this.parentNode.classList.add(\'media--frame\');this.remove()">'
        f'<span class="media__host">{html.escape(host)}</span></div>'
    )


def _file_chip(name: str) -> str:
    return f'<div class="file">📎 {html.escape(name)}</div>'


def render_component(node: dict) -> str:
    kind = node.get("type")
    if kind == TEXT_TYPE:
        return f'<div class="text">{_markdown(node.get("content", ""))}</div>'
    if kind == SECTION_TYPE:
        inner = "".join(render_component(child) for child in node.get("components", []))
        accessory = node.get("accessory") or {}
        thumb = ""
        if accessory.get("type") == THUMBNAIL_TYPE:
            url = (accessory.get("media") or {}).get("url", "")
            thumb = f'<div class="thumb">{_media_frame(url)}</div>'
        return f'<div class="section">{inner}{thumb}</div>'
    if kind == GALLERY_TYPE:
        items = "".join(
            _media_frame((item.get("media") or {}).get("url", ""))
            for item in node.get("items", [])
        )
        return f'<div class="gallery">{items}</div>'
    if kind == FILE_TYPE:
        return _file_chip((node.get("file") or {}).get("url", "file").split("/")[-1])
    if kind == SEPARATOR_TYPE:
        classes = "sep" if node.get("divider") else "gap"
        if int(node.get("spacing", 1) or 1) == 2:
            classes += " large"
        return f'<div class="{classes}"></div>'
    if kind == ROW_TYPE:
        buttons = "".join(
            f'<span class="button">{html.escape(child.get("label", "…"))}</span>'
            for child in node.get("components", [])
            if child.get("label")
        )
        if not buttons:
            buttons = '<span class="button">select</span>'
        return f'<div class="row">{buttons}</div>'
    return f'<div class="text small">[component {kind}]</div>'


def render_card(panel) -> str:
    container = panel.to_components()[0]
    accent = int(container.get("accent_color", 0) or 0)
    body = "".join(render_component(child) for child in container.get("components", []))
    return (
        f'<div class="card" style="--accent:#{accent:06x}">'
        f'<div class="card__bar"></div><div class="card__body">{body}</div></div>'
    )


def _flatten(children) -> list[dict]:
    """Top-level children, with a Section's text lifted out beside its thumbnail.

    A card with a picture beside its text keeps that text inside a Section, so a
    counter that only read the container's own level would report zero headings
    for exactly the cards that have one.
    """
    out = []
    for child in children:
        if child.get("type") == SECTION_TYPE:
            out.extend(_flatten(child.get("components", [])))
        else:
            out.append(child)
    return out


def measure(panel) -> dict:
    """What the card *is*, counted from the JSON rather than described.

    The counts are the argument the review is making: a rule count above one, an
    orphan at either end, a rule closing nothing, two spacers in a row, or an
    accent outside the palette are all visible here as numbers rather than taste.
    """
    container = panel.to_components()[0]
    children = container.get("components", [])
    h2 = h3 = labels = 0
    for child in _flatten(children):
        for line in child.get("content", "").split("\n"):
            if line.startswith("### "):
                h3 += 1
            elif line.startswith("## "):
                h2 += 1
            elif line.startswith("-# "):
                labels += 1
    spacers = [child for child in children if child.get("type") == SEPARATOR_TYPE]
    dividers = [child for child in spacers if child.get("divider")]
    orphan = 0
    dangling = 0
    for index, child in enumerate(children):
        if child.get("type") == SEPARATOR_TYPE and child.get("divider"):
            if index == 0 or index == len(children) - 1:
                orphan += 1
            # A rule with more than one text block under it is not closing
            # anything: the card carries on after the line that was meant to end
            # it. The buttons of a control strip do not count — they are a strip.
            after = [node for node in _flatten(children[index + 1:]) if node.get("type") == TEXT_TYPE]
            if len(after) > 1:
                dangling += 1
    doubles = sum(
        1 for before, after in zip(children, children[1:])
        if before.get("type") == SEPARATOR_TYPE and after.get("type") == SEPARATOR_TYPE
    )
    return {
        "components": len(children),
        "gaps": len(spacers) - len(dividers),
        "rules": len(dividers),
        "orphans": orphan,
        "dangling": dangling,
        "doubles": doubles,
        "h2": h2,
        "h3": h3,
        "labels": labels,
        "accent": "#%06x" % int(container.get("accent_color", 0) or 0),
    }


def metrics_html(measured: dict) -> str:
    bits = [
        f'{measured["components"]} components',
        f'{measured["rules"]} rule{"s" if measured["rules"] != 1 else ""}',
        f'{measured["gaps"]} gap{"s" if measured["gaps"] != 1 else ""}',
        f'{measured["labels"]} label{"s" if measured["labels"] != 1 else ""}',
        f'{measured["h2"]}×h2',
        f'{measured["h3"]}×h3',
        f'accent <code>{measured["accent"]}</code>',
    ]
    if measured["orphans"]:
        bits.append(f'<b class="bad">{measured["orphans"]} orphan rule(s)</b>')
    if measured["dangling"]:
        bits.append(f'<b class="bad">{measured["dangling"]} rule(s) closing nothing</b>')
    if measured["doubles"]:
        bits.append(f'<b class="bad">{measured["doubles"]} double spacer(s)</b>')
    return " · ".join(bits)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Meow · card preview</title>
<style>
  :root {{
    --bg: #313338; --card: #2b2d31; --line: #3f4147; --text: #dbdee1;
    --muted: #949ba4; --link: #00a8fc;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #1e1f22; color: var(--text);
         font: 15px/1.35 "gg sans", "Noto Sans", Helvetica, Arial, sans-serif; }}
  header {{ padding: 28px 32px 8px; max-width: 1180px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 6px; color: #f2f3f5; }}
  .sub {{ color: var(--muted); font-size: 13px; max-width: 880px; }}
  .sub code {{ background: #2b2d31; padding: 1px 5px; border-radius: 4px; }}
  main {{ max-width: 1180px; margin: 0 auto; padding: 8px 32px 60px; }}
  section {{ border-top: 1px solid #2b2d31; padding: 22px 0 8px; }}
  section h2 {{ font-size: 15px; margin: 0 0 4px; color: #f2f3f5; }}
  section .why {{ color: var(--muted); font-size: 13px; margin: 0 0 16px; max-width: 880px; }}
  .pair {{ display: flex; gap: 28px; flex-wrap: wrap; align-items: flex-start; }}
  /* Two columns whenever the pane is wide enough for them, stacked when it is
     not: a card is 420px inside Discord, and nothing here is worth squashing. */
  .side {{ width: 440px; flex: 1 1 440px; min-width: 320px; max-width: 520px; }}
  .side > .label {{ font-size: 11px; letter-spacing: .09em; text-transform: uppercase;
                    color: var(--muted); margin-bottom: 8px; }}
  .metrics {{ font-size: 11.5px; color: var(--muted); margin-top: 10px; line-height: 1.6; }}
  .metrics code {{ background: #2b2d31; padding: 1px 4px; border-radius: 3px; }}
  .bad {{ color: #f0a1a1; }}
  .same {{ color: var(--muted); }}

  /* The card itself, at Discord's own colours and metrics. */
  .card {{ position: relative; background: var(--card); border-radius: 8px;
           overflow: hidden; display: flex; }}
  .card__bar {{ width: 4px; flex: 0 0 4px; background: var(--accent); }}
  .card__body {{ flex: 1; padding: 14px 16px; display: flex; flex-direction: column; }}
  .text p, .text h3, .text h2 {{ margin: 0; }}
  .text h2 {{ font-size: 16px; font-weight: 700; color: #f2f3f5; }}
  .text h3 {{ font-size: 14.5px; font-weight: 700; color: #f2f3f5; }}
  .text p.small {{ font-size: 12px; color: var(--muted); }}
  .text p.quote {{ border-left: 3px solid #4e5058; padding-left: 8px; color: var(--muted); }}
  .text strong {{ color: #f2f3f5; }}
  .text code {{ background: #1e1f22; padding: 1px 5px; border-radius: 4px; font-size: 13px; }}
  .text .link {{ color: var(--link); }}
  .text .mention {{ background: rgba(88,101,242,.3); color: #dee0fc; border-radius: 3px;
                    padding: 0 2px; }}
  .blank {{ height: 8px; }}
  .gap {{ height: 8px; }}
  .gap.large {{ height: 16px; }}
  .sep {{ height: 1px; background: var(--line); margin: 8px 0; }}
  .sep.large {{ margin: 16px 0; }}
  .section {{ display: flex; gap: 12px; align-items: flex-start; }}
  .section .text {{ flex: 1; }}
  .thumb {{ width: 80px; flex: 0 0 80px; }}
  .gallery {{ display: flex; flex-wrap: wrap; gap: 8px; }}
  .media {{ position: relative; width: 100%; min-height: 60px; border-radius: 6px;
            overflow: hidden; background: #232428; display: flex; align-items: center;
            justify-content: center; }}
  .media img {{ width: 100%; max-height: 220px; object-fit: cover; display: block; }}
  .media--frame {{ border: 1px dashed var(--line); }}
  .media__host {{ position: absolute; right: 6px; bottom: 4px; font-size: 10.5px;
                  color: var(--muted); background: rgba(0,0,0,.45); border-radius: 3px;
                  padding: 0 4px; }}
  .media--frame span {{ font-size: 11px; color: var(--muted); }}
  .thumb .media {{ min-height: 80px; height: 80px; }}
  .thumb .media img {{ height: 80px; object-fit: cover; }}
  .file {{ background: #232428; border-radius: 6px; padding: 8px 10px; font-size: 13px;
           color: var(--muted); }}
  .row {{ display: flex; gap: 8px; margin-top: 6px; }}
  .button {{ background: #4e5058; color: #fff; border-radius: 3px; padding: 7px 16px;
             font-size: 13px; font-weight: 500; }}
  footer {{ color: var(--muted); font-size: 12px; text-align: center; padding: 0 32px 40px; }}
</style></head><body>
<header>
  <h1>Meow · Components V2 card preview</h1>
  <p class="sub">Every card below is built by calling <code>utils/style.py</code> and read
  back out of <code>to_components()</code> — the same JSON Discord receives — so the page
  cannot disagree with the code. The “before” column is
  <code>{before_label}</code>, loaded out of git and built through the same fixtures;
  the difference on this page is the difference in the tree.</p>
</header>
<main>{sections}</main>
<footer>rendered by scripts/preview_cards.py · {count} cards · approximate Discord metrics</footer>
</body></html>
"""

SECTION = """
<section>
  <h2>{name}</h2>
  <p class="why">{why}</p>
  <div class="pair">
    <div class="side"><div class="label">{before_label}</div>{before_card}
      <div class="metrics">{before_metrics}</div></div>
    <div class="side"><div class="label">{after_label}</div>{after_card}
      <div class="metrics">{after_metrics}</div></div>
  </div>
</section>
"""


def build_sections(before, after, before_label: str, after_label: str, only: str | None) -> tuple[str, int]:
    out = []
    count = 0
    for key, why, builder in FIXTURES:
        if only and key != only:
            continue
        count += 1
        name = f"{why.split(':')[0]} — {key}"
        before_panel = builder(before)
        after_panel = builder(style)
        before_card_html = render_card(before_panel)
        after_card_html = render_card(after_panel)
        if before_card_html == after_card_html:
            before_card_html = '<p class="metrics same">identical to the new one</p>'
        out.append(SECTION.format(
            name=name, why=why, before_label=before_label, after_label=after_label,
            before_card=before_card_html, before_metrics=metrics_html(measure(before_panel)),
            after_card=after_card_html, after_metrics=metrics_html(measure(after_panel)),
        ))
    return "".join(out), count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--before-rev", default="HEAD",
                        help="git revision to compare against (HEAD by default)")
    parser.add_argument("--only", help="render a single fixture, by key")
    args = parser.parse_args()

    try:
        before = load_revision(args.before_rev, "before")
    except subprocess.CalledProcessError as exc:
        print(f"could not read {args.before_rev}:utils/style.py from git\n{exc.stderr.strip()}")
        return 2

    sections, count = build_sections(before, style, args.before_rev, "working tree", args.only)
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as handle:
        handle.write(PAGE.format(
            before_label=html.escape(args.before_rev),
            sections=sections,
            count=count,
        ))
    print(f"{count} cards → {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
