import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from utils import db

DB_PATH = db.DB_PATH


class Starboard(commands.Cog):
    """Community Starboard for showcasing the best and funniest server moments."""

    starboard_group = app_commands.Group(name="starboard", description="Community Starboard configuration")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

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
        star_count = 0
        for reaction in message.reactions:
            if str(reaction.emoji) in ("⭐", "🌟", "✨"):
                star_count += reaction.count

        if star_count < threshold:
            return

        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT starboard_message_id, stars_count FROM starboard_posts WHERE original_message_id=?",
                (message.id,)
            ) as cur:
                existing = await cur.fetchone()

        header = f"⭐ **{star_count}** | {origin_channel.mention}"

        embed = discord.Embed(
            description=message.content or "",
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

        if existing:
            starboard_msg_id, prev_stars = existing
            try:
                star_msg = await starboard_channel.fetch_message(starboard_msg_id)
                await star_msg.edit(content=header, embed=embed)
                async with aiosqlite.connect(DB_PATH) as conn:
                    await conn.execute("UPDATE starboard_posts SET stars_count=? WHERE original_message_id=?", (star_count, message.id))
                    await conn.commit()
            except Exception:
                pass
        else:
            try:
                star_msg = await starboard_channel.send(content=header, embed=embed)
                async with aiosqlite.connect(DB_PATH) as conn:
                    await conn.execute(
                        "INSERT INTO starboard_posts (original_message_id, starboard_message_id, guild_id, stars_count) VALUES (?, ?, ?, ?)",
                        (message.id, star_msg.id, guild.id, star_count)
                    )
                    await conn.commit()
            except Exception as e:
                print(f"Failed to post to starboard: {e}")

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
        embed.set_footer(text="🐾 Server Starboard • Powered by Meow")
        await interaction.response.send_message(embed=embed)

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
        embed.set_footer(text="🐾 Server Starboard")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Starboard(bot))
