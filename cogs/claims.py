"""``/claim`` — the one-time top-up of level and coins, for a member who asks.

A core command rather than part of *Economy* or *Leveling*, for the reason
``/check`` is core: the two figures it restores belong to the member and not to
a system, and the server least likely to have everything switched on is the one
whose members most need it. The arithmetic and the latch live in
``utils/claims``, so the ceilings can be tested without a Discord connection and
this file is only the answer.
"""
import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from utils import claims
from utils.style import send_card

#: What the picker says. Discord caps a command description at 100 characters,
#: and a constant rather than an f-string so the suite can measure it from the
#: source (``tests/test_static_health.py``) — an f-string is invisible to that
#: check, and the text Discord truncates is text nobody sees the end of.
CLAIM_DESCRIPTION = "Claim your one-time welcome-back coins and level top-up."


class Claims(commands.Cog):
    """One chance per member to claim a floor of coins and level."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="claim", description=CLAIM_DESCRIPTION)
    async def claim(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ `/claim` works inside a server: the coins and the level are that "
                "server's, and so is your one chance at them.",
                ephemeral=True,
            )

        outcome = await asyncio.to_thread(
            claims.claim_once, interaction.guild.id, interaction.user.id
        )

        if outcome.status == claims.ALREADY_CLAIMED:
            return await send_card(
                interaction,
                embed=discord.Embed(
                    title="🎁 Already claimed",
                    description=(
                        "You have already used your one-time claim — it happens "
                        "**once per member**, and it has paid out."
                    ),
                    color=0xED4245,
                ),
                ephemeral=True,
            )

        if outcome.status == claims.NOTHING_TO_CLAIM:
            return await send_card(
                interaction,
                embed=discord.Embed(
                    title="🎁 Nothing to claim",
                    description=(
                        f"Your wallet and your level are already above the one-time "
                        f"top-up (**{claims.MAX_CLAIM_MONEY:,}** 🪙 and "
                        f"**Level {claims.MAX_CLAIM_LEVEL}**), so there is nothing to add.\n"
                        "Your claim is **unused** — nothing was spent, and it is still "
                        "there if you ever fall below the top-up."
                    ),
                    color=0xFEE75C,
                ),
                ephemeral=True,
            )

        # Granted. The virtual card goes up first so awarding roles below cannot
        # push the answer past Discord's three-second window.
        await interaction.response.defer()
        awarded = await self._honour_the_ladder(interaction, outcome.grant.level)

        grant = outcome.grant
        embed = discord.Embed(
            title="🎁 One-time claim",
            description=(
                f"Welcome back. Your one chance has been claimed and it brought you "
                f"up to:\n\n**+{grant.money:,}** 🪙 in your wallet\n"
                f"**+{grant.xp:,}** XP — **Level {grant.level}**"
            ),
            color=0x57F287,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(
            text=(
                "A claim is a floor, not a bonus: it only ever fills the gap up to "
                f"{claims.MAX_CLAIM_MONEY:,} coins and Level {claims.MAX_CLAIM_LEVEL}."
            )
        )
        if awarded:
            embed.add_field(
                name="Level roles",
                value=", ".join(role.name for role in awarded),
                inline=False,
            )
        await send_card(interaction, embed=embed)

    async def _honour_the_ladder(self, interaction, level: int) -> list[discord.Role]:
        """Hand over the level roles the claim just crossed, best-effort.

        Leveling re-checks the ladder when a level *rises*, and a claim is not a
        level-up — so without this a member who claims to Level 25 sits on it
        without the Level 25 role until their next one, which at that level is a
        long wait. Two things are deliberately narrow: it runs only when the
        server has the Leveling system switched on, because its ladder is that
        system's, and every failure is swallowed — the claim is already written,
        and a role the bot cannot hand out must not turn it into an error the
        member reads as "the claim did not work".
        """
        bot = self.bot
        leveling = bot.get_cog("Leveling") if bot is not None else None
        if leveling is None:
            return []
        enabled = getattr(bot, "is_module_enabled", None)
        if callable(enabled) and not enabled(interaction.guild.id, "leveling"):
            return []
        try:
            return await leveling.check_and_award_roles(
                interaction.guild, interaction.user, level
            )
        except Exception as exc:  # noqa: BLE001 — reported, never raised at a member
            print(f"[CLAIM] could not award the level roles: {exc!r}", flush=True)
            return []


async def setup(bot: commands.Bot):
    await bot.add_cog(Claims(bot))
