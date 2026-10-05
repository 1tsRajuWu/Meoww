"""`/decat` — the question goes to the operator's own Mac and comes back answered.

This file is deliberately thin: it is the only piece of the feature that talks to
Discord. Everything it needs from the other machine comes through
``decat.relay`` (the queue) and ``decat.copy`` (the words), and everything it
needs to know about what a good answer looks like it does not need to know at
all — that is the laptop's job.

Three properties are worth stating here rather than in the relay, because they are
about *this* side:

* **The card is the asker's.** A `private` question is deferred ephemerally and
  edited ephemerally; a `public` one is deferred publicly. The refusal lines
  follow the same rule, so "I'm offline rn" lands where the question would have.
* **Nothing about the question is logged.** No ``print`` in this file contains a
  question, an answer or a user id, and ``tests/test_decat.py`` reads this source
  to hold it that way — the deploy's log ring is readable by anybody holding the
  admin passkey, and "the log is how you find out what happened" is not a reason
  to keep somebody's words in it.
* **A refusal is answered before the waiting card goes up.** Replying to an
  interaction commits the asker to waiting just as a defer did; the offline line
  takes about a second and should not arrive as an edit to a card that promised
  the machine was working.
"""
from __future__ import annotations

import asyncio
import time

import discord
from discord import app_commands
from discord.ext import commands

from decat import config, copy, protocol
from decat import relay as relay_module
from decat.card import describe_answer, describe_thinking
from utils.style import edit_card, send_card

__all__ = ["Decat", "build_card", "build_thinking_card", "describe_answer", "short_quote", "setup"]


def short_quote(seed: str) -> tuple[str, str]:
    """One line from the bot's own ``/quote`` corpus, with who said it.

    Imported rather than copied, deliberately: that corpus is a few hundred lines
    somebody maintains in ``cogs/fun.py`` with episode numbers and moods attached,
    and a second copy of it would be a second thing to keep in step — which is the
    failure this repository has a whole class of laws about. The import is inside
    the function because the cog that owns the corpus is only needed while a
    question is in flight, and because a corpus that moved or broke must leave the
    wait with the joke lines rather than nothing at all (hence the ``except``
    around the import: the *thinking card* is not allowed to be a failure point).

    The rule about which lines qualify — short enough to glance at while waiting —
    lives in ``decat.copy`` with the rest of the copy; this function is only the
    borrower.
    """
    try:
        from cogs.fun import QUOTES
    except Exception:  # noqa: BLE001 — a missing corpus is not a broken question
        return "", ""
    return copy.select_quote(QUOTES, seed)


def build_thinking_card(question: str, *, depth: str, seed: str) -> discord.Embed:
    """The card that appears while the Mac works, drawn from ``decat.card``."""
    quote, who = short_quote(seed)
    parts = describe_thinking(question, depth=depth, seed=seed, quote=quote, who=who)
    card = discord.Embed(
        title=parts["title"], description=parts["description"], color=parts["colour"]
    )
    card.set_footer(text=parts["footer"])
    return card


def build_card(answer: protocol.Answer, *, question: str) -> discord.Embed:
    """The card, drawn from the words ``decat.card`` decided on.

    Every rule about *what it says* lives in ``decat/card.py`` so it can be
    checked without this library; what is left here is the drawing.
    """
    parts = describe_answer(answer, question=question)
    card = discord.Embed(
        title=parts["title"], description=parts["description"], color=parts["colour"]
    )
    if parts["sources"]:
        card.add_field(name=copy.SOURCES_TITLE, value=parts["sources"], inline=False)
    card.set_footer(text=parts["footer"])
    return card


class Decat(commands.Cog):
    """The one command, and the gates in front of it."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def relay(self) -> relay_module.Relay:
        return relay_module.shared()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """The module gate, where the rest of this bot keeps it.

        A cog-level check rather than a line inside the callback, for the reason
        every other gated cog gives: this is the shape ``utils/command_scope`` is
        checked against. The registry decides what a server is *offered* and this
        decides what the bot *refuses*, and the two sets have to be the same one
        — a server that has Decat switched off must not keep a command that still
        works (``tests/test_command_scope.py``). Because this runs first, the same
        check in :meth:`gate` is the belt to this braces: it is what a caller that
        is not an interaction sees.
        """
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "decat"):
                await interaction.response.send_message(copy.SYSTEM_OFF, ephemeral=True)
                return False
        return True

    def gate(self, guild_id: int | None) -> str:
        """``""`` when a question may be asked here, else the reason it may not."""
        if not config.token():
            return copy.NOT_CONFIGURED
        if not config.guild_allowed(guild_id):
            return copy.NOT_YOUR_SERVER
        if guild_id is not None and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(guild_id, "decat"):
                return copy.SYSTEM_OFF
        return ""

    async def _say(self, interaction: discord.Interaction, text: str, *, ephemeral: bool) -> None:
        embed = discord.Embed(description=text, color=0xED4245)
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

    @app_commands.command(
        name="decat",
        description="Ask the operator's own Mac a question — it answers with live sources.",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Public — everyone sees it, it may be saved", value="public"),
        app_commands.Choice(name="Private — only you see it, nothing is saved", value="private"),
    ])
    # Two ways to answer, and the asker picks because only they know which one they
    # want: `Deep` is the whole machine — the bigger model and a second pass that
    # re-reads the draft against its sources — and `Fast` is one pass on the small
    # model for a question somebody wants an answer to *now*. The card says which
    # one it was, so `Fast` never borrows the word "checked".
    @app_commands.choices(depth=[
        app_commands.Choice(name="Deep — reads the web, then re-checks itself", value="deep"),
        app_commands.Choice(name="Fast — one quick pass, no re-check", value="fast"),
    ])
    @app_commands.describe(
        question="What you want answered. Be as specific as you can.",
        mode="Whether the answer is saved at all (Private is never saved anywhere).",
        depth="How hard to work on it. Deep is the default; Fast answers sooner.",
        image="A picture to ask about — screenshots, diagrams, photos.",
    )
    async def decat(
        self,
        interaction: discord.Interaction,
        question: str,
        mode: app_commands.Choice[str] | None = None,
        depth: app_commands.Choice[str] | None = None,
        image: discord.Attachment | None = None,
    ):
        mode_value = protocol.parse_mode(mode.value if mode else protocol.MODE_PUBLIC)
        depth_value = protocol.parse_depth(depth.value if depth else protocol.DEPTH_DEEP)
        ephemeral = mode_value == protocol.MODE_PRIVATE

        problem = self.gate(interaction.guild_id)
        if problem:
            return await self._say(interaction, problem, ephemeral=ephemeral)

        text = (question or "").strip()
        limit = config.max_question_chars()
        if len(text) > limit:
            return await self._say(
                interaction, copy.TOO_LONG.format(length=len(text), limit=limit), ephemeral=ephemeral
            )

        # Is the laptop there at all? Asked before anything is deferred, because a
        # deferred response is a promise to answer and this one cannot.
        stats = self.relay.stats()
        age = stats["last_poll_seconds"]
        if age is None or age > config.heartbeat_stale():
            return await self._say(interaction, copy.OFFLINE, ephemeral=ephemeral)
        if stats["state"] == "off":
            return await self._say(interaction, copy.SWITCHED_OFF, ephemeral=ephemeral)

        relay = self.relay
        job = relay_module.new_job(
            text,
            mode=mode_value,
            depth=depth_value,
            guild_id=interaction.guild_id or 0,
            channel_id=interaction.channel_id or 0,
            user_id=interaction.user.id,
            user_name=str(getattr(interaction.user, "display_name", "") or ""),
            image_url=image.url if image is not None else None,
            now=time.time(),
            deadline=config.deadline_seconds(),
        )
        try:
            relay.enqueue(job)
        except relay_module.Refusal as refusal:
            line = copy.COOLDOWN.format(seconds=refusal.message)
            if refusal.kind == "off":
                line = copy.SWITCHED_OFF
            elif refusal.kind == "busy":
                line = copy.BUSY
            return await self._say(interaction, line, ephemeral=ephemeral)

        # The wait gets a card of its own rather than Discord's own deferred
        # placeholder ("Bot is thinking…", in Discord's voice, with nothing on it).
        # It is sent as the *initial* response for the same reason a defer was: the
        # asker is committed to waiting either way, and this way what they are
        # waiting on is written down — which depth, on which machine, and a joke or
        # a quote unless the question is one nobody should be joking about.
        await send_card(
            interaction,
            embed=build_thinking_card(text, depth=depth_value, seed=job.id),
            ephemeral=ephemeral,
        )
        answer = await asyncio.to_thread(relay.wait, job.id, config.deadline_seconds())
        relay.settle(job.id)

        if answer is None:
            card = discord.Embed(description=copy.WENT_QUIET, color=0xED4245)
            return await edit_card(interaction, embed=card)
        await edit_card(interaction, embed=build_card(answer, question=text))


async def setup(bot: commands.Bot):
    await bot.add_cog(Decat(bot))
