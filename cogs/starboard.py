import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from utils import db
from utils.style import send_card, edit_card

DB_PATH = db.DB_PATH


class Starboard(commands.Cog):
    """Community Starboard for showcasing the best and funniest server moments."""

    starboard_group = app_commands.Group(name="starboard", description="Community Starboard configuration")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "starboard"):
                await interaction.response.send_message(
                    "⚠️ The **Starboard** system is currently **turned off** on this server.\n"
                    "An administrator can re-enable it in the [Web Dashboard](https://dacatdashboard.pages.dev/dashboard).",
                    ephemeral=True
                )
                return False
        return True

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if str(payload.emoji) not in ("⭐", "🌟", "✨"):
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        if hasattr(self.bot, "is_module_enabled") and not self.bot.is_module_enabled(guild.id, "starboard"):
            return

        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute("SELECT channel_id, threshold FROM starboard_settings WHERE guild_id=?", (guild.id,)) as cur:
                row = await cur.fetchone()

        if not row:
            return

        starboard_ch_id, threshold = row
        starboard_channel = guild.get_channel(starboard_ch_id)
        if not starboard_channel:
            return

        # Fetch original message
        origin_channel = guild.get_channel(payload.channel_id)
        if not origin_channel:
            return

        # Do not allow starring inside the starboard channel itself
        if origin_channel.id == starboard_ch_id:
            return

        try:
            message = await origin_channel.fetch_message(payload.message_id)
        except Exception:
            return

        # Count stars
        star_count = await self._count_stars(message)

        if star_count < threshold:
            return

        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT starboard_message_id, stars_count FROM starboard_posts WHERE original_message_id=?",
                (message.id,)
            ) as cur:
                existing = await cur.fetchone()

        header = f"⭐ **{star_count}** | {origin_channel.mention}"
        embed = self._build_star_embed(message, header, origin_channel)

        handled = False
        if existing:
            starboard_msg_id, _prev_stars = existing
            try:
                star_msg = await starboard_channel.fetch_message(starboard_msg_id)
                await edit_card(star_msg, content=header, embed=embed)
                async with aiosqlite.connect(DB_PATH) as conn:
                    await conn.execute("UPDATE starboard_posts SET stars_count=? WHERE original_message_id=?", (star_count, message.id))
                    await conn.commit()
                handled = True
            except discord.NotFound:
                # The starboard copy was deleted (channel purged, message removed
                # by hand). The row kept pointing at a dead message, so every later
                # star hit the same NotFound and the post could never come back.
                # Drop the stale row and fall through to re-post it.
                async with aiosqlite.connect(DB_PATH) as conn:
                    await conn.execute("DELETE FROM starboard_posts WHERE original_message_id=?", (message.id,))
                    await conn.commit()
            except Exception:
                handled = True  # transient failure: don't risk a duplicate card

        if not handled:
            try:
                star_msg = await send_card(starboard_channel, content=header, embed=embed)
                async with aiosqlite.connect(DB_PATH) as conn:
                    await conn.execute(
                        "INSERT OR REPLACE INTO starboard_posts (original_message_id, starboard_message_id, guild_id, stars_count) VALUES (?, ?, ?, ?)",
                        (message.id, star_msg.id, guild.id, star_count)
                    )
                    await conn.commit()
            except Exception as e:
                print(f"Failed to post to starboard: {e}")

    async def _count_stars(self, message: discord.Message) -> int:
        """Total star reactions on a message (one definition for add + remove)."""
        return sum(
            reaction.count
            for reaction in message.reactions
            if str(reaction.emoji) in ("⭐", "🌟", "✨")
        )

    def _build_star_embed(self, message: discord.Message, header: str, origin_channel) -> discord.Embed:
        """Build the starboard card for a message."""
        # The Message Content intent can be off, so `message.content` may be empty.
        # Show something readable instead of a blank card.
        if message.content:
            description = message.content
        elif message.attachments:
            description = "\n".join(f"📎 {a.filename}" for a in message.attachments)
        elif message.embeds:
            description = "🔗 Shared a link"
        else:
            description = "*[message text unavailable]*"

        embed = discord.Embed(
            description=description,
            color=0xFEE75C,
            timestamp=message.created_at
        )
        embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
        embed.add_field(name="Source", value=f"[Jump to Message]({message.jump_url})", inline=False)
        embed.set_footer(text=f"ID: {message.id}")

        # Check for image attachments or embed images
        if message.attachments:
            att = message.attachments[0]
            if att.content_type and att.content_type.startswith("image/"):
                embed.set_image(url=att.url)
        elif message.embeds:
            for emb in message.embeds:
                if emb.image:
                    embed.set_image(url=emb.image.url)
                    break
        return embed

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        """Re-sync the starboard when a star is removed.

        Only additions used to be handled, so a card's count could only ever climb
        and a featured post could never come back down.
        """
        if str(payload.emoji) not in ("⭐", "🌟", "✨"):
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        if hasattr(self.bot, "is_module_enabled") and not self.bot.is_module_enabled(guild.id, "starboard"):
            return

        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute("SELECT channel_id, threshold FROM starboard_settings WHERE guild_id=?", (guild.id,)) as cur:
                row = await cur.fetchone()
            if not row:
                return
            starboard_ch_id, threshold = row
            async with conn.execute(
                "SELECT starboard_message_id FROM starboard_posts WHERE original_message_id=?",
                (payload.message_id,)
            ) as cur:
                existing = await cur.fetchone()

        starboard_channel = guild.get_channel(starboard_ch_id)
        origin_channel = guild.get_channel(payload.channel_id)
        if not starboard_channel or not origin_channel or origin_channel.id == starboard_ch_id:
            return

        try:
            message = await origin_channel.fetch_message(payload.message_id)
        except Exception:
            return

        star_count = await self._count_stars(message)

        header = f"⭐ **{star_count}** | {origin_channel.mention}"
        embed = self._build_star_embed(message, header, origin_channel)

        if star_count >= threshold:
            if not existing:
                return
            try:
                star_msg = await starboard_channel.fetch_message(existing[0])
                await edit_card(star_msg, content=header, embed=embed)
                async with aiosqlite.connect(DB_PATH) as conn:
                    await conn.execute(
                        "UPDATE starboard_posts SET stars_count=? WHERE original_message_id=?",
                        (star_count, payload.message_id),
                    )
                    await conn.commit()
            except discord.NotFound:
                # The starboard copy was deleted by hand or by a channel purge.
                # Drop the stale row and re-post, so the card and the channel both
                # match reality. The add path already self-healed; this one used to
                # keep the row pointing at a dead message forever.
                async with aiosqlite.connect(DB_PATH) as conn:
                    await conn.execute("DELETE FROM starboard_posts WHERE original_message_id=?", (payload.message_id,))
                    await conn.commit()
                try:
                    star_msg = await send_card(starboard_channel, content=header, embed=embed)
                    async with aiosqlite.connect(DB_PATH) as conn:
                        await conn.execute(
                            "INSERT OR REPLACE INTO starboard_posts (original_message_id, starboard_message_id, guild_id, stars_count) VALUES (?, ?, ?, ?)",
                            (payload.message_id, star_msg.id, guild.id, star_count),
                        )
                        await conn.commit()
                except Exception as e:
                    print(f"Failed to re-post to starboard: {e}")
            except Exception as e:
                print(f"[STARBOARD] Could not update the card for {payload.message_id}: {e}")
            return

        if not existing:
            return

        # Fell below the threshold — retire the card so the channel matches reality.
        try:
            star_msg = await starboard_channel.fetch_message(existing[0])
            await star_msg.delete()
        except discord.NotFound:
            pass  # already gone; the stale row below still needs dropping
        except Exception as e:
            # Could not remove the card (usually missing Manage Messages). Keep the
            # row: a later sync can still edit this card, whereas dropping the row
            # now would make the next star post a duplicate next to it.
            print(f"[STARBOARD] Could not retire the card for {payload.message_id}: {e}")
            return
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute("DELETE FROM starboard_posts WHERE original_message_id=?", (payload.message_id,))
            await conn.commit()

    @starboard_group.command(name="set", description="Set the Starboard showcase channel and reaction threshold.")
    @app_commands.describe(
        channel="The text channel where star moments will be featured",
        threshold="Number of stars required (minimum 1, default 3)"
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def starboard_set(self, interaction: discord.Interaction, channel: discord.TextChannel, threshold: app_commands.Range[int, 1, 20] = 3):
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO starboard_settings (guild_id, channel_id, threshold) VALUES (?, ?, ?)",
                (interaction.guild.id, channel.id, threshold)
            )
            await conn.commit()

        embed = discord.Embed(
            title="⭐ Starboard Activated!",
            description=(
                f"The community Starboard is now live!\n\n"
                f"> 📌 **Starboard Channel:** {channel.mention}\n"
                f"> ⭐ **Threshold:** `{threshold}` stars\n\n"
                f"*When members react with ⭐ to messages, the best ones will be pinned to {channel.mention}!*"
            ),
            color=0xFEE75C,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="Server Starboard • Powered by Meow")
        await send_card(interaction, embed=embed)

    @starboard_group.command(name="status", description="Check current Starboard configuration.")
    async def starboard_status(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute("SELECT channel_id, threshold FROM starboard_settings WHERE guild_id=?", (interaction.guild.id,)) as cur:
                row = await cur.fetchone()

        if not row:
            return await interaction.response.send_message("❌ Starboard has not been configured in this server. Use `/starboard set` to configure it!", ephemeral=True)

        ch_id, thresh = row
        ch = interaction.guild.get_channel(ch_id)
        ch_mention = ch.mention if ch else f"Unknown ({ch_id})"

        embed = discord.Embed(
            title="⭐ Starboard Status",
            description=(
                f"> 📌 **Showcase Channel:** {ch_mention}\n"
                f"> ⭐ **Required Stars:** `{thresh}` ⭐"
            ),
            color=0xFEE75C,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="Server Starboard")
        await send_card(interaction, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Starboard(bot))
