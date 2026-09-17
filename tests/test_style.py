"""Every non-interactive message now goes through the Components V2 helpers.
Discord rejects a payload that exceeds its hard limits, which would take a
command down, so the budget guards are pinned here."""
import unittest

import discord

from utils import style


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
        self.assertIn("**Name**\nValue", rendered)
        self.assertIn("Body only", rendered)


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


class RegistryIndependence(unittest.TestCase):
    def test_style_exports_survive(self):
        """The panels/helpers other cogs import must keep existing."""
        for name in ("Panel", "send_panel", "edit_to_panel", "send_card", "edit_card",
                     "card", "success", "error", "info", "warn", "progress_bar"):
            self.assertTrue(hasattr(style, name), f"missing export: {name}")
        for name in ("ACCENT", "PINK", "SUCCESS", "DANGER", "GOLD", "NIGHT", "INFO",
                     "WARN", "NEUTRAL"):
            self.assertIsInstance(getattr(style, name), int)


if __name__ == "__main__":
    unittest.main()
