import discord
from discord import app_commands
from discord.ext import commands

class Help(commands.Cog):
    """Lists all available commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Show all available commands.")
    async def help_cmd(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🐾 Meow — Command Guide",
            description="Here's everything I can do. Use `/` to get started!",
            color=0x7289DA,
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=interaction.client.user.display_avatar.url)

        embed.add_field(
            name="🔨 Moderation",
            value="`/ban` `/unban` `/kick` `/mute` `/unmute` `/warn` `/warnings` `/clearwarnings` `/purge` `/modlog`",
            inline=False,
        )
        embed.add_field(
            name="🎫 Tickets",
            value="`/ticket-setup` `/ticket-add` `/ticket-remove`",
            inline=False,
        )
        embed.add_field(
            name="🛠️ Server Control",
            value="`/serverinfo` `/userinfo` `/lockdown` `/unlock` `/lockdown-server` `/unlock-server` `/slowmode` `/addrole` `/removerole` `/announce`",
            inline=False,
        )
        embed.add_field(
            name="👋 Welcome",
            value="`/setwelcome` `/setgoodbye`",
            inline=False,
        )
        embed.add_field(
            name="🎭 Reaction Roles",
            value="`/reactionrole-add`",
            inline=False,
        )
        embed.add_field(
            name="🛡️ Auto-Mod",
            value="`/automod-invites` `/automod-antispam`",
            inline=False,
        )
        embed.add_field(
            name="💰 Economy",
            value="`/balance` `/daily` `/weekly` `/work` `/pay` `/rob` `/deposit` `/withdraw` `/richlist` `/addmoney` `/removemoney` `/setmoney` `/resetmoney`",
            inline=False,
        )
        embed.add_field(
            name="🎮 Games & Gambling",
            value="`/crash` `/mines` `/roulette` `/highlow` `/tictactoe` `/blackjack` `/slots` `/coinflip` `/dice` `/rps` `/trivia` `/8ball`",
            inline=False,
        )
        embed.add_field(
            name="🎵 Music & Voice",
            value="`/radio` `/play` `/pause` `/resume` `/skip` `/stop` `/queue` `/nowplaying` `/volume` `/songinfo` `/lyrics`",
            inline=False,
        )
        embed.add_field(
            name="🐾 OwO Zoo & Actions",
            value="`/hunt` `/zoo` `/sellanimals` `/bonk` `/slap` `/hug` `/kiss` `/pat` `/punch` `/bite` `/cuddle` `/poke` `/feed` `/marry` `/divorce` `/ship` `/owo`",
            inline=False,
        )
        embed.add_field(
            name="⭐ Levels, Roles & Activity",
            value="`/rank` `/leaderboard` `/levelroles` `/addlevelrole` `/removelevelrole` `/levelsettings` `/setlevel` `/givexp` `/resetlevels`",
            inline=False,
        )
        embed.add_field(
            name="🐱 Fun & Quotes",
            value="`/meme` `/meow` `/meowsound` `/kms` `/tan` `/quote` `/addquote` `/customquote` `/customquotes` `/delquote`",
            inline=False,
        )
        embed.add_field(
            name="⚖️ Legal",
            value="`/privacy` `/tos`",
            inline=False,
        )
        embed.add_field(
            name="⚙️ Bot Config",
            value="`/setavatar` `/setbanner`",
            inline=False,
        )
        embed.set_footer(text="🐾 Meow Bot • Use / to run any command", icon_url=interaction.client.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
