"""The step between a typed command and an irreversible change.

Broad permission grants are a risk category, not a feature, and the difference
between a powerful bot and one that is a single misremembered option away from a
wiped server is entirely in this file. Two rules, and neither is optional:

* **A ceiling.** Every action that destroys things declares how many it may touch
  without a second step. Below the ceiling the count *is* the guard: deleting the
  one channel somebody named is what they asked for, and a prompt for it trains
  people to click through prompts. Above it the action is a different thing —
  forty channels is not "delete that one, forty times" — and it does not run.
* **A confirmation, from the person who asked.** The prompt is ephemeral, so it
  only exists for the invoker; it is answered by a press, never by a timeout; and
  a press from anybody else is refused rather than counted. A confirmation that a
  bystander can give is decoration, and the whole point of the layer is that the
  bot cannot be talked into a mass deletion by whoever is fastest to click.

A ceiling is per action and per server — a server with 300 channels is not a
server with 5 — so it is a stored setting with a declared default rather than a
constant, and this module is the only reader of it.

What this is **not**: a permission model. Who may run the command at all is
Discord's own permission check (``manage_channels``, ``manage_roles``) plus the
permission table in ``utils/permissions.py``. This is the second gate, on the
size of what they asked for.
"""
from __future__ import annotations

import discord

from utils import db, features

#: How long a confirmation stays open. Long enough to read what it says, short
#: enough that a prompt nobody answered is not still live an hour later — the
#: thing it is guarding is a mass deletion, and a stale "yes" is exactly the
#: failure mode a timeout avoids.
TIMEOUT_SECONDS = 60.0

#: The declared ceiling per action: how many subjects one command may touch
#: without the confirmation step. Keyed by the same action names
#: ``utils.structure`` uses, so the two cannot disagree about what an action is
#: called — and now *declared* in the feature registry rather than here, because
#: the dashboard draws these four as controls and a second copy of a default is
#: how the page and the guard come to disagree about what "unchanged" means.
#: The registry is stdlib-only, so this import cannot cycle.
CEILINGS: dict[str, int] = {
    spec.id: int(spec.default)
    for spec in features.settings_for("structure")
}

#: The largest ceiling a server may store, from the widest bound the registry
#: declares for one. Kept as a module constant because this is the reader most
#: callers import.
MAX_CEILING = max(
    (spec.maximum or 0) for spec in features.settings_for("structure")
) or 200


#: Overridable per server, per action, as a stored setting. The key is this
#: module's own namespace so a hand-edited database cannot collide with another
#: feature's setting.
SETTING = "confirmation_ceiling"

#: ``<SETTING>.<action>`` — the shape the registry declares per ceiling.
def setting_key(action: str) -> str:
    return features.setting_key("structure", action)


def ceiling_for(guild_id: int | None, action: str) -> int:
    """How many of ``action`` this server allows without a confirmation.

    Read from the guild's stored override, falling back to the declared default
    for the action. A value that is not a number, or is outside the range, is
    treated as absent rather than clamped: a corrupted setting must not silently
    become a *larger* limit than the default, which is the only direction of
    corruption that matters here.
    """
    default = CEILINGS.get(action, 1)
    if not guild_id:
        return default
    raw = _stored(guild_id, action)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < 0 or value > MAX_CEILING:
        print(
            f"[GUARD] guild {guild_id} has {SETTING}.{action}={raw!r}, which is out "
            f"of range; using the default ({default}).",
            flush=True,
        )
        return default
    return value


def _stored(guild_id: int, action: str):
    """The stored override, if this process can read one. Never raises."""
    reader = getattr(db, "read_setting_sync", None)
    if reader is None:  # pragma: no cover - every deployment has it
        return None
    try:
        return reader(guild_id, f"{SETTING}.{action}")
    except Exception as exc:  # noqa: BLE001 - a failed read is not a bigger limit
        print(f"[GUARD] could not read the ceiling for {action}: {exc}", flush=True)
        return None


def needed(count: int, ceiling: int) -> bool:
    """Whether this many subjects needs the confirmation step.

    Pure, because this is the rule rather than the prompt, and a rule that only
    exists inside a UI callback is one nobody can check.
    """
    return int(count) > int(ceiling)


class Confirmation(discord.ui.View):
    """The prompt: ephemeral, one press, and only the asker's press counts.

    ``finished`` stays ``None`` until somebody decides, so a timeout is
    distinguishable from a refusal — ``guard`` treats them the same way, but the
    view is also what the tests drive, and "nobody answered" is a different fact
    from "somebody said no".
    """

    def __init__(self, invoker_id: int, *, subject: str, count: int, timeout: float = TIMEOUT_SECONDS):
        super().__init__(timeout=timeout)
        self.invoker_id = invoker_id
        self.subject = subject
        self.count = count
        self.finished: bool | None = None
        #: How many presses were refused because they came from somebody else.
        self.refused_presses = 0

    async def _decide(self, interaction: discord.Interaction, decision: bool) -> None:
        if getattr(interaction.user, "id", None) != self.invoker_id:
            self.refused_presses += 1
            await interaction.response.send_message(
                "That confirmation is not yours to give — whoever ran the command "
                "has to answer it.",
                ephemeral=True,
            )
            return
        self.finished = decision
        self.stop()
        await interaction.response.edit_message(
            content=(
                f"{'✅ Confirmed' if decision else '🚫 Cancelled'} — "
                f"{self.count} × {self.subject}."
            ),
            view=None,
        )

    @discord.ui.button(label="Yes, do it", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._decide(interaction, True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._decide(interaction, False)


def prompt_body(action: str, subject: str, count: int, ceiling: int) -> str:
    """What the prompt says, composed once.

    It names the number, the ceiling that was exceeded and what cannot be undone,
    because a confirmation that does not say what it is about is a button people
    press to make the message go away.
    """
    return (
        f"⚠️ **`{action}` would touch {count} thing{'s' if count != 1 else ''}** "
        f"({subject}). This server's limit for that is **{ceiling}**, so it needs "
        "saying out loud.\n"
        "**This cannot be undone**, and Discord keeps no copy to restore from.\n"
        f"-# Press *Yes, do it* to go ahead — the prompt expires in "
        f"{int(TIMEOUT_SECONDS)} seconds and does nothing on its own."
    )


async def guard(
    interaction: discord.Interaction,
    *,
    action: str,
    count: int,
    subject: str = "",
    ceiling: int | None = None,
) -> bool:
    """Whether to go ahead: ``True`` without asking, ``False`` never answered.

    The ceiling is read once here rather than by each caller, so a command cannot
    accidentally check one limit and report another. Below the ceiling nothing is
    sent: a prompt for a one-channel delete is noise, and the noise is what makes
    the *real* prompt get clicked without reading.
    """
    limit = ceiling if ceiling is not None else ceiling_for(interaction.guild_id, action)
    if not needed(count, limit):
        return True

    view = Confirmation(
        int(getattr(interaction.user, "id", 0) or 0),
        subject=subject or action,
        count=int(count),
    )
    await interaction.response.send_message(
        prompt_body(action, subject or action, int(count), limit),
        view=view,
        ephemeral=True,
    )
    await view.wait()
    return bool(view.finished)
