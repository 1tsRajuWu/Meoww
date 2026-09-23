"""Every non-interactive message now goes through the Components V2 helpers.
Discord rejects a payload that exceeds its hard limits, which would take a
command down, so the budget guards are pinned here."""
import re
import unittest
from pathlib import Path

import discord

from utils import style

ROOT = Path(__file__).resolve().parent.parent

#: A real clip the soundboard ships, so the payload is built from a real file
#: rather than from a path that could be anything.
SAMPLE_CLIP = ROOT / "assets" / "sounds" / "meow_1.ogg"


def _collect_text(node, out):
    """Walk a serialized component tree collecting every text body."""
    if isinstance(node, dict):
        content = node.get("content")
        if isinstance(content, str):
            out.append(content)
        for value in node.values():
            _collect_text(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_text(value, out)
    return out


def _view_text(view) -> str:
    if hasattr(view, "to_components"):
        try:
            return "".join(_collect_text(view.to_components(), []))
        except Exception:
            pass
    parts = []

    def walk(obj):
        content = getattr(obj, "content", None)
        if isinstance(content, str):
            parts.append(content)
        for child in getattr(obj, "children", []) or []:
            walk(child)

    walk(view)
    return "".join(parts)


class StylePrimitives(unittest.TestCase):
    def test_clamp_respects_limit(self):
        self.assertEqual(style._clamp("abc", 10), "abc")
        clamped = style._clamp("x" * 100, 10)
        self.assertLessEqual(len(clamped), 10)
        self.assertTrue(clamped.endswith("…"))
        self.assertEqual(style._clamp("abc", 0), "")

    def test_clamp_handles_none(self):
        self.assertEqual(style._clamp(None, 10), "")

    def test_progress_bar_bounds(self):
        self.assertIn("0%", style.progress_bar(0, 100))
        self.assertIn("50%", style.progress_bar(50, 100))
        self.assertIn("100%", style.progress_bar(100, 100))
        self.assertIn("100%", style.progress_bar(500, 100), "must clamp above the total")
        self.assertIn("0%", style.progress_bar(-5, 100), "must clamp below zero")
        self.assertIn("0%", style.progress_bar(5, 0), "must survive a zero total")
        self.assertIn("0%", style.progress_bar("nonsense", 100))

    def test_field_text_skips_blanks(self):
        self.assertEqual(style.field_text(None), "")
        self.assertEqual(style.field_text([("", "")]), "")
        rendered = style.field_text([("Name", "Value"), ("", "Body only")])
        self.assertIn("-# Name\nValue", rendered)
        self.assertIn("Body only", rendered)

    def test_a_field_label_is_quieter_than_its_value(self):
        """Before this, both were the same weight — so a card of eight fields was
        eight bold labels and eight plain values with no subject."""
        rendered = style.field_text([("Level", "12"), ("XP", "1,240")])
        for line in rendered.splitlines():
            if line.startswith("-#"):
                self.assertNotIn("**", line, "a label is not also bold")
        self.assertNotIn("**", rendered)
        self.assertEqual(rendered.count("-#"), 2)

    def test_a_label_with_no_value_keeps_its_label(self):
        """An empty field means \"not applicable\", and the label is what says
        which thing is not applicable."""
        rendered = style.field_text([("Loyalty", "")])
        self.assertEqual(rendered, "-# Loyalty")


class EmbedConversion(unittest.TestCase):
    def test_none_embed_returns_none(self):
        self.assertIsNone(style.embed_to_layout(None))

    def test_empty_embed_returns_none(self):
        self.assertIsNone(style.embed_to_layout(discord.Embed()))

    def test_title_only_embed_converts(self):
        panel = style.embed_to_layout(discord.Embed(title="Hello"))
        self.assertIsNotNone(panel)
        self.assertIn("Hello", _view_text(panel))

    def test_rich_embed_preserves_content(self):
        embed = discord.Embed(
            title="Rank Up", description="You reached level 5", color=0x57F287
        )
        embed.add_field(name="XP", value="1200")
        embed.set_footer(text="Server Level System")
        panel = style.embed_to_layout(embed)
        text = _view_text(panel)
        self.assertIn("Rank Up", text)
        self.assertIn("You reached level 5", text)
        self.assertIn("1200", text)
        self.assertIn("Server Level System", text)

    def test_oversized_embed_is_clamped_to_budget(self):
        embed = discord.Embed(title="Huge", description="x" * 12000)
        embed.add_field(name="Field", value="y" * 8000)
        panel = style.embed_to_layout(embed)
        self.assertIsNotNone(panel)
        self.assertLessEqual(len(_view_text(panel)), style.MAX_V2_CHARS)

    def test_oversized_card_is_clamped_to_budget(self):
        panel = style.card(
            title="T" * 500,
            body="B" * 20000,
            footer="F" * 500,
            fields=[("N" * 200, "V" * 5000)],
        )
        self.assertLessEqual(len(_view_text(panel)), style.MAX_V2_CHARS)

    def test_semantic_helpers_build_cards(self):
        for builder in (style.success, style.error, style.info, style.warn):
            panel = builder("body text")
            self.assertIsNotNone(panel)
            self.assertIn("body text", _view_text(panel))


class _EditTarget:
    """A Message stand-in that enforces discord.py's own payload rule.

    ``handle_message_parameters`` rejects a payload carrying ``embed`` and
    ``embeds`` at once, checking for the *keys* rather than the values — so a
    payload with both cannot be sent even when one of them is ``None``.
    """

    def __init__(self, *, components_v2: bool = False, break_layout: bool = False):
        self.flags = type("Flags", (), {"components_v2": components_v2})()
        self.break_layout = break_layout
        self.calls: list[dict] = []

    async def edit(self, **kwargs):
        if "embed" in kwargs and "embeds" in kwargs:
            raise TypeError("Cannot mix embed and embeds keyword arguments.")
        if self.break_layout and "view" in kwargs:
            raise RuntimeError("the layout was rejected")
        self.calls.append(kwargs)
        return self


class CardEdits(unittest.IsolatedAsyncioTestCase):
    """Editing is how a card is kept current, so its payload has to be sendable."""

    async def test_an_upgrade_clears_the_legacy_payload_with_one_embed_keyword(self):
        target = _EditTarget()
        await style.edit_card(target, embed=discord.Embed(title="hi", description="there"))
        self.assertEqual(len(target.calls), 1)
        payload = target.calls[0]
        self.assertNotIn("embed", payload)
        self.assertEqual(payload["embeds"], [])
        self.assertIsNone(payload["content"])
        self.assertIsNotNone(payload["view"])

    async def test_a_view_edit_never_carries_both_embed_keywords(self):
        target = _EditTarget()
        await style.edit_card(target, content="hi", embed=discord.Embed(title="x"), view=object())
        self.assertEqual(len(target.calls), 1)
        self.assertNotIn("embeds", target.calls[0])

    async def test_a_legacy_message_still_falls_back_to_a_plain_edit(self):
        target = _EditTarget(break_layout=True)
        await style.edit_card(target, embed=discord.Embed(title="hi"))
        self.assertEqual(len(target.calls), 1)
        self.assertIn("embed", target.calls[0])
        self.assertNotIn("view", target.calls[0])
        self.assertNotIn("embeds", target.calls[0])

    async def test_a_card_that_cannot_be_edited_reports_why_not_the_retry(self):
        # Discord refuses an embed on a card, so the retry cannot succeed — and it
        # used to replace the real error with that rejection.
        target = _EditTarget(components_v2=True, break_layout=True)
        with self.assertRaises(RuntimeError):
            await style.edit_card(target, embed=discord.Embed(title="hi"))
        self.assertEqual(target.calls, [])


class _SendTarget:
    """A Messageable stand-in that records what a card payload carried."""

    def __init__(self):
        self.calls = []

    async def send(self, **kwargs):
        self.calls.append(kwargs)
        return self


def _component_types(view) -> list[int]:
    """Every nested component type in a layout, flattened."""
    def walk(node):
        if isinstance(node, dict):
            if "type" in node:
                yield node["type"]
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    return list(walk(view.to_components()))


class AttachmentsOnACard(unittest.IsolatedAsyncioTestCase):
    """A Components V2 message shows an attachment only when a component
    references it. That is why the soundboard had "no sound": the clip uploaded
    fine and Discord drew nothing, with no error anywhere."""

    FILE_COMPONENT = 13

    async def test_a_file_is_referenced_and_sent(self):
        target = _SendTarget()
        attachment = discord.File("assets/sounds/purr.ogg", filename="purr.ogg")
        await style.send_card(target, content="listen", file=attachment)
        payload = target.calls[0]
        self.assertIs(payload.get("file"), attachment, "the upload must travel with the card")
        view = payload["view"]
        self.assertIn(self.FILE_COMPONENT, _component_types(view))
        self.assertIn("attachment://purr.ogg", str(view.to_components()))

    async def test_several_files_are_sent_as_a_list(self):
        target = _SendTarget()
        files = [
            discord.File("assets/sounds/purr.ogg", filename="a.ogg"),
            discord.File("assets/sounds/meow_1.ogg", filename="b.ogg"),
        ]
        await style.send_card(target, content="listen", files=files)
        self.assertEqual(target.calls[0].get("files"), files)
        self.assertNotIn("file", target.calls[0])

    async def test_an_embed_card_still_carries_its_file(self):
        target = _SendTarget()
        await style.send_card(
            target,
            embed=discord.Embed(title="Sound", description="clip"),
            file=discord.File("assets/sounds/purr.ogg", filename="purr.ogg"),
        )
        payload = target.calls[0]
        self.assertIn("file", payload)
        self.assertNotIn("embeds", payload)
        self.assertIn(self.FILE_COMPONENT, _component_types(payload["view"]))

    async def test_a_card_without_files_is_unchanged(self):
        target = _SendTarget()
        await style.send_card(target, content="hello")
        self.assertNotIn("file", target.calls[0])
        self.assertNotIn(self.FILE_COMPONENT, _component_types(target.calls[0]["view"]))

    async def test_an_image_the_embed_already_shows_is_not_drawn_twice(self):
        """``/market chart`` sets its picture as ``attachment://chart.png``, which
        the MediaGallery references. Referencing it again as a file would show the
        same chart twice."""
        target = _SendTarget()
        embed = discord.Embed(title="BTC", description="chart")
        embed.set_image(url="attachment://chart.png")
        attachment = discord.File("assets/fonts/Roboto-Regular.ttf", filename="chart.png")
        await style.send_card(target, embed=embed, file=attachment)
        payload = target.calls[0]
        self.assertIn("attachment://chart.png", str(payload["view"].to_components()))
        self.assertNotIn(self.FILE_COMPONENT, _component_types(payload["view"]))
        self.assertIs(payload["file"], attachment, "the upload still travels")

    def test_only_unreferenced_uploads_are_listed(self):
        referenced = discord.File("assets/fonts/Roboto-Regular.ttf", filename="chart.png")
        loose = discord.File("assets/sounds/purr.ogg", filename="purr.ogg")
        kept = style.unreferenced_files([referenced, loose], ["attachment://chart.png"])
        self.assertEqual(kept, [loose])
        self.assertEqual(style.unreferenced_files([loose], None), [loose])
        self.assertEqual(style.unreferenced_files(None, ["attachment://x.png"]), [])

    def test_a_file_component_is_skipped_rather_than_guessed_at(self):
        self.assertEqual(style.file_components(["not a file"]), [])
        self.assertEqual(style.file_components(None), [])


class PlainMessagesDrawThemselves(unittest.IsolatedAsyncioTestCase):
    """``/meowsound``'s player, and the whole of why ``send_plain`` exists.

    A Components V2 layout is what draws a card, and a File component has no
    player: the soundboard's clip came out as a file row with a download arrow —
    *"why is there download, I don't wanna download, I wanna hear"*. On a payload
    that is not a layout the **client** draws the attachment, with a play button,
    so the clip goes out on a plain message. That message is now the only one the
    command sends: giving the clip a message of its own to get a player cost the
    card's picture and a second message, which the next report was about — *"why
    double, I only want one like it was"*.
    """

    async def test_the_clip_goes_out_on_a_payload_with_no_layout_of_ours(self):
        target = _SendTarget()
        attachment = discord.File(SAMPLE_CLIP, filename="purr.ogg")
        embed = discord.Embed(title="🔊 Mew!", description="**Sound:** purr")
        await style.send_plain(target, embed=embed, file=attachment)
        payload = target.calls[0]
        self.assertIs(payload["file"], attachment)
        self.assertIs(payload["embed"], embed, "the card travels with the clip")
        self.assertNotIn("view", payload, "a card is what draws the download row")
        self.assertNotIn("flags", payload)

    async def test_a_plain_message_cannot_ping_by_accident(self):
        """It goes through the same dispatcher as every card, so the mentions
        default is the same one — a payload with no card is not a payload with no
        rule."""
        target = _SendTarget()
        await style.send_plain(target, content="🔊 purr")
        self.assertIs(target.calls[0]["allowed_mentions"], style.NO_PINGS)


class MentionsOnACard(unittest.TestCase):
    """The opt-in that /action, /rizz and /roast needed."""

    def test_pings_names_its_users_and_nothing_else(self):
        alice = discord.Object(id=1)
        allowed = style.pings(alice)
        self.assertEqual([u.id for u in allowed.users], [1])
        self.assertFalse(allowed.everyone, "a ping set is never @everyone")
        self.assertFalse(allowed.roles, "and never a role")
        self.assertEqual(allowed.replied_user, False)

    def test_no_users_means_silence(self):
        """A caller with nobody to name gets nothing notified, rather than an
        accidental @everyone."""
        for empty in (style.pings(), style.pings(None)):
            self.assertEqual(list(empty.users), [])
            self.assertFalse(empty.everyone)

    def test_the_default_is_still_silence(self):
        """``AllowedMentions.none()`` blanks every list — that is the whole point
        of it being the default on every other card in this bot."""
        for field in ("everyone", "users", "roles", "replied_user"):
            self.assertFalse(getattr(style.NO_PINGS, field), field)


class TheAuthorPicture(unittest.TestCase):
    def test_a_card_with_nothing_to_draw_uses_the_authors_icon(self):
        embed = discord.Embed(title="Welcome", description="hi")
        embed.set_author(name="Meow", icon_url="https://example.invalid/bot.png")
        panel = style.embed_to_layout(embed)
        self.assertIn("https://example.invalid/bot.png", str(panel.to_components()))

    def test_a_real_thumbnail_still_wins_over_the_author_icon(self):
        embed = discord.Embed(title="Welcome", description="hi")
        embed.set_author(name="Meow", icon_url="https://example.invalid/bot.png")
        embed.set_thumbnail(url="https://example.invalid/thumb.png")
        panel = style.embed_to_layout(embed)
        rendered = str(panel.to_components())
        self.assertIn("https://example.invalid/thumb.png", rendered)
        self.assertNotIn("https://example.invalid/bot.png", rendered)


# --------------------------------------------------------------------------- #
# The typographic pass
# --------------------------------------------------------------------------- #
def _children(panel) -> list[dict]:
    """The container's children, as Discord would receive them."""
    return panel.to_components()[0]["components"]


def _spacers(children) -> list[dict]:
    return [node for node in children if node.get("type") == 14]


def _headings(children, level: int = 2) -> list[str]:
    """Lines that *are* a heading of this level, rather than merely containing one.

    ``str.count("## ")`` cannot tell ``## Title`` from ``### Group``, because the
    second contains the first as a substring — which is exactly the assertion this
    helper exists to make honestly.
    """
    marker = "#" * level + " "
    out = []
    for node in children:
        for line in node.get("content", "").splitlines():
            if line.startswith(marker) and not line.startswith("#" * (level + 1) + " "):
                out.append(line)
    return out


class TheTypeScale(unittest.TestCase):
    """One rung per job, so a card cannot be four things at one volume."""

    def test_a_card_says_its_title_as_a_level_two_heading(self):
        self.assertEqual(style.title("Rank Up"), "## Rank Up")
        self.assertEqual(style.section("Members"), "### Members")
        self.assertEqual(style.label("XP"), "-# XP")
        self.assertEqual(style.subtext("XP"), style.label("XP"), "the footer is a label")
        self.assertEqual(style.heading("Kept"), style.title("Kept"), "the old name still works")
        for empty in ("", None):
            self.assertEqual(style.title(empty), "")
            self.assertEqual(style.label(empty), "")

    def test_the_title_is_the_only_level_two_heading_on_a_card(self):
        """An embed's title plus a description that opens with its own ``##``
        arrived as two subjects in one card and the card argued with itself."""
        panel = style.card(title="Wallet", body="## Balance\n1,200 🪙")
        children = _children(panel)
        self.assertEqual(_headings(children), ["## Wallet"], "exactly one subject")
        self.assertEqual(_headings(children, 3), ["### Balance"])

    def test_the_same_rule_applies_to_a_converted_embed(self):
        embed = discord.Embed(title="Leaderboard", description="## This week\n1. alice")
        children = _children(style.embed_to_layout(embed))
        self.assertEqual(_headings(children), ["## Leaderboard"])
        self.assertEqual(_headings(children, 3), ["### This week"])

    def test_an_embeds_author_is_an_eyebrow_above_the_title(self):
        embed = discord.Embed(title="Welcome", description="hi")
        embed.set_author(name="Meow")
        content = "".join(node.get("content", "") for node in _children(style.embed_to_layout(embed)))
        self.assertLess(content.index("-# Meow"), content.index("## Welcome"), content)


class TheSpacingScale(unittest.TestCase):
    """Air and rules are placed by the builder, never by hand."""

    def test_the_four_levels_rank_from_air_to_a_strong_rule(self):
        ranks = [style._SPACER_RANK[style._spacer_shape(node)] for node in
                 (style.GAP, style.BREAK, style.RULE, style.RULE_LARGE)]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(len(set(ranks)), 4, "each level has to mean something new")

    def test_a_card_never_opens_or_closes_with_a_spacer(self):
        for panel in (
            style.card(title="T", body="b"),
            style.card(body="b", footer="f"),
            style.card(fields=[("A", "1")]),
            style.card(gallery_urls=["https://x.test/a.png"]),
            style.card(title="T", body="b", fields=[("A", "1")], footer="f",
                       gallery_urls=["https://x.test/a.png"]),
        ):
            children = _children(panel)
            self.assertNotEqual(children[0].get("type"), 14, "a card cannot start with air")
            self.assertNotEqual(children[-1].get("type"), 14, "nor end with it")

    def test_two_spacers_never_sit_side_by_side(self):
        panel = style.card(title="T", body="b", fields=[("A", "1")])
        shapes = [style._spacer_shape(node) for node in _children(panel)]
        for before, after in zip(shapes, shapes[1:]):
            self.assertFalse(before is not None and after is not None, shapes)

    def test_a_card_with_a_footer_has_exactly_one_rule(self):
        children = _children(style.card(title="T", body="b", fields=[("A", "1")], footer="f"))
        rules = [node for node in _spacers(children) if node.get("divider")]
        self.assertEqual(len(rules), 1, children)
        # ...and it is immediately above the footer, separating it from content.
        index = children.index(rules[0])
        self.assertIn("-# f", children[index + 1]["content"])

    def test_a_card_without_a_footer_has_no_rule(self):
        for panel in (style.card(title="T", body="b"), style.card(fields=[("A", "1")])):
            self.assertEqual([n for n in _spacers(_children(panel)) if n.get("divider")], [])

    def test_a_rule_a_caller_adds_is_never_an_orphan(self):
        """The helper is the only thing that may place a separator, and it refuses
        two in a row, so an extra one is absorbed rather than doubled."""
        children: list = []
        style._place(children, style.TextDisplay("a"))
        style._place(children, style.GAP)
        style._place(children, style.RULE)
        self.assertEqual(len(children), 2, "the gap and the rule collapsed into one")
        self.assertEqual(style._spacer_shape(children[1]), (True, False), "the rule wins")

    def test_air_before_nothing_is_not_placed(self):
        children: list = []
        style._place(children, style.BREAK)
        self.assertEqual(children, [], "a leading gap would be air under the title")


class OneAccent(unittest.TestCase):
    """A card has one accent, and only a state may be something other than it."""

    def test_the_palette_is_one_accent_and_four_states(self):
        self.assertEqual(set(style.ACCENTS), {"accent", "success", "warn", "danger", "info"})
        self.assertEqual(style.ACCENTS["accent"], style.ACCENT)
        for role, value in style.ACCENTS.items():
            self.assertEqual(style.accent_for(value), value, role)

    def test_a_cog_colour_becomes_the_accent(self):
        """251 ``0x`` literals live in the cogs and not one of them consulted this
        table, so the bar was a different colour from one command to the next."""
        for stray in (0xE91E63, 0xEB459E, 0x111827, 0x34495E, 0x95A5A6, 0xF1C40F + 1):
            self.assertEqual(style.accent_for(stray), style.ACCENT, hex(stray))

    def test_a_state_survives_however_it_was_spelled(self):
        cases = {
            style.SUCCESS: (0x57F287, 0x2ECC71),
            style.DANGER: (0xED4245, 0xE74C3C),
            style.WARN: (0xFEE75C, 0xF39C12),
            style.INFO: (0x3498DB,),
        }
        for role, spellings in cases.items():
            for value in spellings:
                self.assertEqual(style.accent_for(value), role, hex(value))

    def test_nonsense_becomes_the_accent_rather_than_a_colour_that_is_not_one(self):
        for value in (None, True, "5865f2", -1, 0x1000000, 1.5, object()):
            self.assertEqual(style.accent_for(value), style.ACCENT, repr(value))
        self.assertEqual(style.accent_for(0xE91E63, default=style.DANGER), style.DANGER)

    def test_a_card_sets_its_accent_once_on_the_container(self):
        panel = style.card(title="T", body="b", accent=0xE91E63)
        self.assertEqual(panel.accent, style.ACCENT)
        container = panel.to_components()[0]
        self.assertEqual(container["accent_color"], style.ACCENT)
        self.assertNotIn("color", str(_children(panel)), "no second colour inside the card")

    def test_every_colour_the_help_panel_names_lands_in_the_palette(self):
        """Twelve decorative category colours were twelve accents on one screen;
        they go through the palette like any other card's does."""
        from cogs import help as help_cog

        roles = set(style.ACCENTS.values())
        for key, module in help_cog.HELP_MODULES.items():
            self.assertIn(style.accent_for(module["color"]), roles, key)

    def test_no_card_is_handed_a_colour_the_palette_does_not_know(self):
        """A card cannot invent a sixth accent by writing a hex literal.

        This walks the tree rather than one card, because the failure is a *new*
        call site: ``accent=0xEB459E`` reads as harmless and is a differently
        coloured product one command at a time.
        """
        literal = re.compile(r"accent(?:_color)?\s*=\s*0[xX][0-9A-Fa-f]{6}")
        offenders = []
        paths = [ROOT / "bot.py", ROOT / "keep_alive.py"]
        for folder in ("cogs", "utils"):
            paths.extend(sorted((ROOT / folder).glob("*.py")))
        for path in paths:
            if path.name == "style.py":  # the palette is where the literals live
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if literal.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "an accent has to come from the palette")

    def test_every_semantic_helper_keeps_its_state(self):
        for builder, role in (
            (style.success, style.SUCCESS), (style.error, style.DANGER),
            (style.warn, style.WARN), (style.info, style.INFO),
        ):
            self.assertEqual(builder("b").accent, role)


class TextHygiene(unittest.TestCase):
    """What a body arrives with, and what the card does about it."""

    def test_edges_are_trimmed_and_runs_of_blank_lines_collapsed(self):
        self.assertEqual(style.clean_text("\n\nonly\n\n\n\nbody\n\n"), "only\n\nbody")
        self.assertEqual(style.clean_text("   \n\n"), "")
        self.assertEqual(style.clean_text(None), "")
        self.assertEqual(style.clean_text("trailing spaces   \nnext"), "trailing spaces\nnext")

    def test_a_hand_drawn_divider_is_removed(self):
        """It used to land directly above the hairline the card draws itself."""
        for drawn in ("---", "━━━━━━", "──────", "______", "=====", "▬▬▬▬", "* * *"):
            self.assertEqual(style.clean_text(f"above\n{drawn}\nbelow"), "above\nbelow", drawn)

    def test_a_dash_that_means_something_survives(self):
        for kept in ("- a bullet", "— an em dash", "51 - 49", "a-b-c"):
            self.assertEqual(style.clean_text(kept), kept)

    def test_a_body_cannot_leave_air_under_the_title(self):
        panel = style.card(title="T", body="\n\nbody\n\n")
        self.assertEqual(_children(panel)[0]["content"], "## T\nbody")

    def test_the_footer_is_the_last_thing_on_the_card(self):
        for body in ("b\n\n\n", "b\n----\n", "b"):
            children = _children(style.card(title="T", body=body, footer="f"))
            self.assertIn("-# f", children[-1]["content"], children)

    def test_a_card_of_nothing_still_has_a_component(self):
        children = _children(style.card(title="", body="   "))
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["type"], 10)


class ContentAddedLater(unittest.TestCase):
    """Merged embeds and route extras go above the closing rule."""

    def _footer_index(self, panel) -> int:
        children = _children(panel)
        return next(i for i, node in enumerate(children) if node.get("content", "").startswith("-# "))

    def test_appended_content_lands_above_the_footer(self):
        panel = style.card(title="T", body="b", footer="f")
        panel.append(style.TextDisplay("late"))
        children = _children(panel)
        names = [node.get("content", "") for node in children]
        self.assertLess(names.index("late"), names.index("-# f"), names)

    def test_a_merged_embeds_later_fields_are_not_stranded_under_the_rule(self):
        first = discord.Embed(title="First", description="one")
        first.set_footer(text="foot")
        second = discord.Embed(title="Second", description="two")
        panel = style.embeds_to_layout([first, second])
        children = _children(panel)
        names = [node.get("content", "") for node in children]
        self.assertIn("-# Second\ntwo", names, names)
        self.assertLess(names.index("-# Second\ntwo"), names.index("-# foot"), names)
        self.assertEqual(children[-1]["content"], "-# foot", "the footer is still last")

    def test_a_routes_extra_content_lands_above_the_footer(self):
        view = style._layout_from(
            discord.Embed(title="T", description="b").set_footer(text="f"), None, "extra", 180
        )
        children = _children(view)
        names = [node.get("content", "") for node in children]
        self.assertLess(names.index("extra"), names.index("-# f"), names)

    def test_a_row_stays_below_the_footer(self):
        panel = style.card(title="T", body="b", footer="f")
        panel.add_row(discord.ui.Button(label="ok"))
        children = _children(panel)
        self.assertEqual(children[-1]["type"], 1, "the control strip is last")
        self.assertIn("-# f", children[-2]["content"])

    def test_appending_to_a_footerless_card_keeps_the_end_clean(self):
        panel = style.card(title="T", body="b")
        panel.append(style.TextDisplay("more"))
        children = _children(panel)
        self.assertEqual(children[-1].get("content"), "more")
        self.assertEqual([n for n in children if n.get("type") == 14], [])


class RegistryIndependence(unittest.TestCase):
    def test_style_exports_survive(self):
        """The panels/helpers other cogs import must keep existing."""
        for name in ("Panel", "send_panel", "edit_to_panel", "send_card", "edit_card",
                     "card", "success", "error", "info", "warn", "progress_bar",
                     "title", "heading", "section", "label", "subtext", "clean_text",
                     "accent_for", "ACCENTS", "ACCENT_ALIASES", "GAP", "BREAK", "RULE"):
            self.assertTrue(hasattr(style, name), f"missing export: {name}")
        for name in ("ACCENT", "SUCCESS", "DANGER", "INFO", "WARN"):
            self.assertIsInstance(getattr(style, name), int)


if __name__ == "__main__":
    unittest.main()
