"""The support bridge's Discord half: the replies, coming back.

A message an operator sends from the dashboard or the console is posted into the
support channel (and written down) by the route that owns it. This cog is the
other direction, and it is one listener: when somebody writes in that channel —
or in a thread on it, because an answer written in a thread is still an answer —
the message is taken into the conversation, so the next render of the card shows
it.

It also does the one thing a conversation needs and a log does not: **it opens a
thread on the message that started it**, says where the conversation continues,
and writes the thread's id onto the row. Support is one person's problem, and a
room full of them interleaved is neither side's idea of an answer — the thread is
what makes the back-and-forth possible, and its id is what lets the console reply
*into* the conversation instead of linking the operator out to Discord.

A thread is only opened for a message that arrives **live**. A pull of the
channel's history takes in what people already said, and turning fifty old
messages into fifty threads would be the bridge rearranging a room nobody asked
it to touch.

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
            # this listener, and the unique index is where those two meet. It is
            # also why a thread is not opened here: a message a pull took in
            # reads as known, and the room it came from is history.
            return
        where = f" (thread: {row['where_name']})" if row["where_name"] else ""
        print(
            f"[SUPPORT] ✉️ {row['author_name']} wrote in the support channel{where}"
            " — it is on the dashboard now.",
            flush=True,
        )
        if not row["thread_id"]:
            await self._open_thread(message, row)

    async def _open_thread(self, message, row: dict) -> None:
        """Open the conversation's own room, or say why this bot cannot.

        Every failure below is printed and *survived*: a bridge that could not
        make a thread is still a bridge that is recording the conversation, and
        taking the listener down would lose the reply as well as the thread. The
        thread is opened on the message that started it, so the member finds it
        by looking at their own message — which is where they will look.
        """
        try:
            thread = await message.create_thread(
                name=support.thread_name(row["body"], row["author_name"]),
                auto_archive_duration=1440,
            )
        except Exception as exc:  # noqa: BLE001 — a refusal, never a lost reply
            print(
                "[SUPPORT] ⚠️ Could not open a thread on that message "
                f"({type(exc).__name__}: {exc}). The conversation is still being "
                "recorded — the bot needs Create Public Threads in the channel.",
                flush=True,
            )
            return
        await support.attach_thread(row["id"], getattr(thread, "id", 0))
        try:
            await thread.send(support.CONTINUE_HERE)
        except Exception as exc:  # noqa: BLE001 — the thread exists either way
            print(f"[SUPPORT] Could not speak inside the new thread: {exc}", flush=True)
        print(
            f"[SUPPORT] 🧵 Opened a thread on {row['author_name']}'s message — "
            "the conversation continues there, from either side.",
            flush=True,
        )


async def setup(bot):
    await bot.add_cog(Support(bot))
