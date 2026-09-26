"""The support bridge's Discord half: the replies, coming back.

A message an operator sends from the dashboard or the console is posted into the
support channel (and written down) by the route that owns it. This cog is the
other direction, and it is one listener: when somebody writes in that channel —
or in a thread on it, because an answer written in a thread is still an answer —
the message is taken into the conversation, so the next render of the card shows
it.

**What is decided here is nothing.** Whether a message belongs in the
conversation is ``utils/support.is_reply``: the channel, the thread parent, the
author being a person rather than the bot, another bot or a webhook. That rule
is pure and tested against stand-ins, and this file holds only the two things a
pure module cannot do — read the live message, and write the row. Keeping the
decision out of the listener is what makes "the bridge answered itself" a
failure a test can find rather than something the operator notices one day.

There is no module gate and there is deliberately no per-server switch. This is
not a server's feature: the channel is a deployment setting
(``SUPPORT_CHANNEL_ID``), the conversation belongs to the operator, and a server
that never asked for anything gets nothing from this listener — it only ever
looks at messages in the one channel the deployment named.

The one thing worth knowing when replies arrive **empty**: reading a guild
message's content needs the Message Content intent. With it dropped, this
listener sees a message whose ``content`` is empty and nothing about that is
visible here — so the row keeps the attachment count and the card says which of
the two it was (`utils/support.reading`), because a blank line in a conversation
reads as somebody having written nothing.
"""
from __future__ import annotations

import discord
from discord.ext import commands

from utils import support


class Support(commands.Cog):
    """Takes what people write in the support channel into the conversation."""

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """One reply, written down once. See ``utils/support`` for the rules."""
        if not support.is_reply(message):
            return
        row = await support.record_inbound(message)
        if row is None:
            # Already known — the same id can arrive from a pull as well as from
            # this listener, and the unique index is where those two meet.
            return
        where = f" (thread: {row['where_name']})" if row["where_name"] else ""
        print(
            f"[SUPPORT] ✉️ {row['author_name']} replied in the support channel{where}"
            " — it is on the dashboard now.",
            flush=True,
        )


async def setup(bot):
    await bot.add_cog(Support(bot))
