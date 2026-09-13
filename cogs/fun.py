import discord
from discord import app_commands
from discord.ext import commands
import random
import asyncio
import aiohttp
import aiosqlite
import datetime
from utils import db

DB_PATH = db.DB_PATH

async def setup_custom_quotes_table():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""CREATE TABLE IF NOT EXISTS custom_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            quote TEXT,
            author TEXT,
            source TEXT,
            category TEXT DEFAULT 'funny',
            created_at TEXT
        )""")
        await conn.commit()

KMS_RESPONSES = [
    # Anime Comedy Fails
    ("💥", "Yamcha crater pose unlocked. The Saibaman didn't even use a named attack, he just looked at me aggressively. Don't even bother gathering the Dragon Balls."),
    ("🥖", "'Nah, I'd win' I said with 100% confidence before getting sliced clean in half like a supermarket baguette. Sukuna is laughing in the corner."),
    ("🌪️", "Mr. Stark... I don't feel so good... *turns into a cloud of Dorito dust and gets sucked directly into a Roomba*"),
    ("🍩", "Got donuted straight through the chest like Ace protecting Luffy. Tell mom I died with a smile and zero coins in my wallet. 🔥"),
    ("📓", "Light Yagami wrote my name in the Death Note, but had to erase it twice because he was laughing too hard at my haircut. 🥔"),
    ("🚚", "Tried to do the Naruto run across the street, tripped on my own shoelace, and got hit by Truck-kun. Reincarnated as a broken toaster in another world."),
    ("🌧️", "Stood in the pouring rain looking up at the sky like Vegeta after getting folded by an Android, but then my umbrella broke inside out."),
    ("🧹", "Challenged Levi Ackerman to a fistfight because I thought 'he's only 5'3, what's the worst that could happen?'. Currently in ICU."),
    ("🧠", "Hit with Gojo's Unlimited Void. My brain has completely dissolved into 2-minute Maggi noodles. Absolute cinema."),
    ("⏳", "Subaru died for the 89th time this afternoon. The respawn sound effect is ringing in my ears on a 10-hour loop."),
    ("👁️", "Lelouch vi Britannia commands you... to delete my browser search history immediately before the paramedics unlock my phone."),
    ("🫡", "Gave an inspiring 10-minute anime speech about friendship and determination, and then immediately got stepped on by a 15-meter titan."),
    ("👁️", "Tried to awaken my Mangekyo Sharingan by glaring aggressively into the bathroom mirror. Just ended up with dry eyes and a migraine."),
    ("😴", "Caught in the Infinite Tsukuyomi except my dream is just me doing unpaid overtime with 250ms ping."),
    ("🥫", "Died and got isekai'd, but I reincarnated as a dented vending machine in an empty parking lot. Pure skill issue."),
    ("⚡", "Kakashi hit me with Chidori. He didn't even remember my name. Just called me 'NPC #4'."),

    # Movie & Pop Culture Comedy
    ("🚢", "Rose wouldn't scoot 2 inches to the left on that giant wooden door. Floating in freezing water re-evaluating my rizz. ❄️"),
    ("👍", "Sinking into molten lava giving a slow thumbs-up like the Terminator. Hasta la vista, I forgot to turn off the stove. 🌋"),
    ("🗡️", "Did a 40-second flashy sword spinning routine like Indiana Jones' opponent, just to get casually taken down with a pebble."),
    ("🥀", "Look what they did to my boy... Vito Corleone took one look at my bank balance and began weeping openly."),
    ("🛋️", "Fell down the stairs like Peter Griffin. Laying on the floor holding my knee going 'ssssss... aaaaahhh... ssssss... aaaaahhh' for 45 minutes."),
    ("🧱", "Wile E. Coyote painted a hyper-realistic tunnel on the wall. I sprinted into it at full speed. Flat as a pancake."),
    ("⚡", "Emperor Palpatine hit me with Unlimited Power because I interrupted his monologue to ask for the Wi-Fi password."),
    ("🥊", "Apollo Creed vs Ivan Drago moment. He said 'If he dies, he dies.' And folks, I definitely died."),
    ("🌌", "Watched attack ships on fire off the shoulder of Orion... all those moments lost like my sleep schedule. Time to die."),
    ("📱", "You either die a hero, or you live long enough to see yourself get left on 'Delivered' for 14 hours."),
    ("🎭", "Joker pushed me into a vat of toxic chemicals. Now I'm dead, broke, AND bright neon green."),

    # Everyday Relatable & Meme Fails
    ("🍽️", "Said 'You too!' when the waiter told me to enjoy my meal. Walked directly into oncoming traffic to escape the shame."),
    ("🏜️", "Swallowed a dry Popeyes biscuit with no water. Throat is drier than the Sahara desert. Flatlining on the kitchen rug."),
    ("🧱", "Stepped on a Lego brick in the pitch dark. My soul immediately detached from my body and ascended to the clouds."),
    ("🦟", "A mosquito buzzed near my ear at 3 AM. I slapped myself in the face at Mach 3. Died of blunt force self-inflicted trauma."),
    ("🚪", "Mom yelled my full government name including my middle name from downstairs. My heart simply stopped out of respect."),
    ("🥤", "Choked on my own saliva while sitting completely still doing absolutely nothing. Peak human evolution."),
    ("🪑", "Attempted a cool parkour roll off the bed, miscalculated gravity, and folded in half like a cheap lawn chair."),
    ("🥔", "Opened the chip bag at 2 AM trying to be silent. The crunch echoed across three time zones. Mauled by the demons."),
    ("📉", "Looked at my crypto portfolio for 2 seconds. The red candles blinded me. Currently decomposing."),
    ("🧠", "Remembered an awkward conversation from 2019 while trying to fall asleep. My brain self-destructed from pure cringe."),
    ("🎬", "My ghost watched the paramedics arrive, rate my dramatic slow-motion fall a 4/10, and leave without helping."),
    ("🪽", "Drank Red Bull expecting wings. Just got severe heart palpitations and a one-way ticket to the pearly gates."),
    ("☀️", "Accidentally opened Discord in light mode at 3 AM. Retinas incinerated on contact. Rest in peace."),
    ("🪙", "Bet my life savings on coinflip, landed on tails when I called heads, and combusted into dust on the spot."),
    ("📶", "Wifi went down for 4 minutes. Had to confront my own thoughts. Did not survive."),
    ("🔋", "Phone fell on my face in bed from maximum height. Nose shattered. Life revoked.")
]

OPENINGS = [
    "I just self-deleted with maximum theatrical drama.",
    "Fell into an unskippable 20-minute death cutscene.",
    "Accidentally hit the self-destruct button on my own life.",
    "I have departed the mortal lobby permanently.",
    "Died of terminal, incurable cringe.",
    "Flatlined in the most embarrassing way imaginable.",
    "Perished like an unnamed anime background character in episode 1.",
    "Took 9999 emotional damage from a single thought.",
    "I threw myself into the abyss and the abyss threw me right back.",
    "Fell into a slow-motion death scene with a Japanese orchestra playing.",
    "Folded under pressure like a cheap umbrella in a typhoon.",
    "Suffered a critical emotional malfunction.",
]

COMEDY_SITUATIONS = [
    "Laying on the floor in the iconic Peter Griffin stair-fall pose.",
    "The crime scene looks like a Michael Bay action explosion with zero budget.",
    "Surrounded by 4 empty energy drinks, an open Discord tab, and regrets.",
    "The cat walked over my body, sniffed my forehead, and knocked over a glass of water.",
    "A slow-motion 4K camera captured every millisecond of my dramatic downfall.",
    "My ghost is currently floating above my body facepalming aggressively.",
    "A sad violin soundtrack started playing from an unknown source in the ceiling.",
    "Looking like a defeated video game boss slowly dissolving into red pixels.",
    "The drama level in here is currently exceeding toxic Chernobyl limits.",
]

ABSURD_DETAILS = [
    "Someone please delete my browser search history before the paramedics unlock my phone.",
    "Mom walked in, looked at my corpse, and said 'it's because of that damn phone'.",
    "The coroner opened my tab, saw 54 unread Discord pings, and whispered 'valid'.",
    "Left a 2-word note on the fridge that just says 'skill issue'.",
    "Paramedics arrived, hit the Yamcha pose in solidarity, and drove away.",
    "My last words were literally 'watch this guys, trust me I know what I'm doing'.",
    "CSI showed up with crime tape and I'm posing my corpse for better thumbnail angles.",
    "My ghost is haunting this exact channel. You will never be free of me.",
    "The dog sighed heavily, realized no one is feeding him dinner now, and walked away.",
]

CLOSERS = [
    "10/10 death scene, would perish again. 🎬",
    "Sayonara cruel world, catch me in the next isekai. 🌸",
    "Press F to pay respects or PayPal me funds. 🕯️",
    "Tell my WiFi router I loved it like a brother. 📶",
    "Tombstone will read: 'He died as he lived: making poor choices.' 🪦",
    "Do not disturb, currently rotting and vibing. ☠️",
    "Skill issue of cosmic, multidimensional proportions. 🌟",
    "Yare yare daze... 🧢",
    "Nah, I in fact did NOT win. 💀",
    "Gg no re, life is uninstalled. 🎮",
]

def get_kms_response():
    # 70% chance of curated movie/anime/meme comedy reference, 30% modular
    if random.random() < 0.70:
        return random.choice(KMS_RESPONSES)
    
    icon = random.choice(["💀", "☠️", "🪦", "😵", "🤡", "💥", "🎬"])
    opening = random.choice(OPENINGS)
    situation = random.choice(COMEDY_SITUATIONS)
    detail = random.choice(ABSURD_DETAILS)
    closer = random.choice(CLOSERS)
    
    return icon, f"{opening} {situation} {detail} {closer}"

CAT_SOUNDS = [
    ("meow_1.ogg", "🐾 Short Cute Meow", "Mew!"),
    ("meow_2.ogg", "🐾 Soft Domestic Meow", "Meoww~"),
    ("meow_3.ogg", "🐾 Pleading Kitten Meow", "Mrrrp?"),
    ("meow_4.ogg", "🐾 Loud Vocal Meow", "MREOWWW!"),
    ("meow_5.ogg", "🐾 Chattering Cat Meows", "Nya nya nya!"),
    ("meow_classic_mp3.mp3", "🐾 Classic Crisp Meow", "Meeeee-ow!"),
    ("meow_impatient.ogg", "🐾 Impatient Food Meow", "Meow! Feed me!"),
    ("meow_kitten.ogg", "🐾 Baby Kitten Squeak", "Squeak-mew!"),
    ("meow_purrmeow.ogg", "🐾 Purr-Meow Combo", "Prrr-meww!"),
    ("meow_whine.ogg", "🐾 Dramatic Whiny Meow", "Mewwwoooo..."),
    ("meow_wild.ogg", "🐾 Wild Forest Cat Chirp", "Chirp-mew!"),
    ("purr.ogg", "🐾 Deep Cozy Engine Purr", "*purrrrrrrrr*"),
    ("purr_bertie.ogg", "🐾 Gentle Sleeping Purr", "*soft purr...*"),
]


class CustomQuoteView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id

    @discord.ui.button(label="🎲 Another Custom Quote", style=discord.ButtonStyle.secondary)
    async def another_quote(self, interaction: discord.Interaction, button: discord.ui.Button):
        await setup_custom_quotes_table()
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT id, user_id, quote, author, source, category, created_at FROM custom_quotes WHERE guild_id=? ORDER BY RANDOM() LIMIT 1",
                (self.guild_id,)
            ) as cur:
                row = await cur.fetchone()

        if not row:
            return await interaction.response.send_message("❌ No custom quotes found in this server!", ephemeral=True)

        qid, uid, qtext, qauthor, qsource, qcat, qdate = row
        submitter = interaction.guild.get_member(uid) if interaction.guild else None
        sub_name = submitter.display_name if submitter else f"User {uid}"

        cat_emojis = {
            "funny": "😂",
            "love": "💖",
            "deep": "🧠",
            "out_of_context": "🔥",
            "legendary": "🏆"
        }
        icon = cat_emojis.get(qcat, "📜")

        embed = discord.Embed(
            title=f"{icon} Server Custom Quote #{qid}",
            description=f"### “{qtext}”",
            color=0xF1C40F,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="👤 Quoted", value=f"**{qauthor}**", inline=True)
        if qsource:
            embed.add_field(name="📍 Context / Source", value=f"*{qsource}*", inline=True)
        embed.set_footer(text=f"Submitted by {sub_name} • {qdate}")

        await interaction.response.edit_message(embed=embed, view=self)


# -------------------------------------------------------------------------
# Meme System & Curated Viral Content
# -------------------------------------------------------------------------

MEME_SUBREDDITS = {
    "random": ["memes", "dankmemes", "wholesomememes", "me_irl", "comedyheaven", "shitposting", "gaming"],
    "dank": ["dankmemes", "shitposting"],
    "gaming": ["gaming", "pcmasterrace"],
    "anime": ["animemes", "goodanimemes"],
    "wholesome": ["wholesomememes"],
    "relatable": ["me_irl"],
    "tech": ["ProgrammerHumor", "pcmasterrace"]
}

FALLBACK_MEMES = [
    {
        "title": "When the code works on the first try and you don't trust it",
        "url": "https://i.imgur.com/2s8K6Hh.jpeg",
        "author": "CodeCat",
        "subreddit": "ProgrammerHumor",
        "postLink": "https://reddit.com/r/ProgrammerHumor",
        "ups": 18450
    },
    {
        "title": "Task failed successfully",
        "url": "https://i.imgur.com/8xV3a6k.jpeg",
        "author": "BugHunter",
        "subreddit": "memes",
        "postLink": "https://reddit.com/r/memes",
        "ups": 29800
    },
    {
        "title": "Me watching the loading screen tips like they're ancient scriptures",
        "url": "https://i.imgur.com/4qu9lkj.gif",
        "author": "GamerCat",
        "subreddit": "gaming",
        "postLink": "https://reddit.com/r/gaming",
        "ups": 14200
    },
    {
        "title": "Bro really thought he had full control over his life schedule",
        "url": "https://i.imgur.com/cc8u642.png",
        "author": "NightOwl",
        "subreddit": "me_irl",
        "postLink": "https://reddit.com/r/me_irl",
        "ups": 21300
    },
    {
        "title": "My cat at 3 AM explaining why the empty bowl is an emergency",
        "url": "https://i.imgur.com/rgkf914.png",
        "author": "FelineOverlord",
        "subreddit": "wholesomememes",
        "postLink": "https://reddit.com/r/wholesomememes",
        "ups": 31500
    },
    {
        "title": "The GPU was the entire gaming PC",
        "url": "https://i.redd.it/iu60iio3rsoh1.png",
        "author": "PCBuilder",
        "subreddit": "pcmasterrace",
        "postLink": "https://reddit.com/r/pcmasterrace",
        "ups": 10121
    }
]

async def fetch_meme(category: str = "random") -> dict:
    subreddits = MEME_SUBREDDITS.get(category.lower(), MEME_SUBREDDITS["random"])
    sub = random.choice(subreddits)
    url = f"https://meme-api.com/gimme/{sub}"
    headers = {"User-Agent": "MeowDiscordBot/2.0 (by /u/1tsRajOwO)"}

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            for _ in range(4):
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # Ensure not NSFW, not spoiler, and has valid image url
                        if not data.get("nsfw", False) and not data.get("spoiler", False) and data.get("url"):
                            return data
    except Exception as e:
        print(f"Error fetching meme from {url}: {e}")

    return random.choice(FALLBACK_MEMES)

def build_meme_embed(meme_data: dict, category: str = "random") -> discord.Embed:
    title = meme_data.get("title", "Fresh Meme")
    url = meme_data.get("url", "")
    subreddit = meme_data.get("subreddit", "memes")
    author = meme_data.get("author", "unknown")
    ups = meme_data.get("ups", 0)
    post_link = meme_data.get("postLink", "")

    embed = discord.Embed(
        title=title[:256],
        url=post_link if post_link else None,
        color=0xFF4500,
        timestamp=discord.utils.utcnow()
    )
    if url:
        embed.set_image(url=url)

    embed.set_author(
        name=f"r/{subreddit} • u/{author}",
        icon_url="https://www.redditstatic.com/desktop2x/img/favicon/android-icon-192x192.png"
    )
    ups_str = f"{ups:,}" if isinstance(ups, int) and ups > 0 else "Top Rated"
    embed.set_footer(
        text=f"👍 {ups_str} upvotes • Category: {category.title()} • 🐾 Meow Memes"
    )
    return embed

class MemeView(discord.ui.View):
    def __init__(self, category: str = "random", post_link: str = None):
        super().__init__(timeout=300)
        self.category = category
        if post_link:
            self.add_item(discord.ui.Button(label="Reddit Post", url=post_link, style=discord.ButtonStyle.link, emoji="🔗"))

    @discord.ui.button(label="Next Meme", style=discord.ButtonStyle.primary, emoji="🔄", custom_id="next_meme_btn")
    async def next_meme(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        meme_data = await fetch_meme(self.category)
        embed = build_meme_embed(meme_data, self.category)
        new_view = MemeView(category=self.category, post_link=meme_data.get("postLink"))
        await interaction.edit_original_response(embed=embed, view=new_view)


class Fun(commands.Cog):
    """Fun and entertainment commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.recent_tan = []
        self.recent_sounds = []
        self.recent_quotes = []

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            if not self.bot.is_module_enabled(interaction.guild_id, "fun"):
                await interaction.response.send_message(
                    "⚠️ The **Fun & Memes** system is currently **turned off** on this server.\n"
                    "An administrator can re-enable it in the [Web Dashboard](https://meow-discord-bot.onrender.com/dashboard).",
                    ephemeral=True
                )
                return False
        return True

    @app_commands.command(name="meme", description="Get the best viral memes from Reddit and across the internet!")
    @app_commands.describe(
        category="Choose a meme genre (random, dank, gaming, anime, wholesome, relatable, tech)"
    )
    @app_commands.choices(category=[
        app_commands.Choice(name="🎲 Random (Best Overall)", value="random"),
        app_commands.Choice(name="🔥 Dank Memes", value="dank"),
        app_commands.Choice(name="🎮 Gaming Memes", value="gaming"),
        app_commands.Choice(name="🎌 Anime Memes", value="anime"),
        app_commands.Choice(name="💖 Wholesome Memes", value="wholesome"),
        app_commands.Choice(name="💀 Relatable (me_irl)", value="relatable"),
        app_commands.Choice(name="💻 Tech & Programming", value="tech")
    ])
    async def meme(self, interaction: discord.Interaction, category: app_commands.Choice[str] = None):
        await interaction.response.defer()
        cat_val = category.value if category else "random"
        meme_data = await fetch_meme(cat_val)
        embed = build_meme_embed(meme_data, cat_val)
        view = MemeView(category=cat_val, post_link=meme_data.get("postLink"))
        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="kms", description="Having a rough day? Let the world know dramatically.")
    async def kms(self, interaction: discord.Interaction):
        icon, message = get_kms_response()
        
        embed = discord.Embed(
            color=0xED4245,
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(
            name=f"{interaction.user.display_name} has perished",
            icon_url=interaction.user.display_avatar.url
        )
        embed.description = f"## {icon} {message}"
        embed.set_footer(text=f"🐾 RIP {interaction.user.display_name} • Unlimited Drama", icon_url=interaction.user.display_avatar.url)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        # Send publicly without any time limit (stays forever!)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="meow", description="Get a random meow and a cat pic!")
    async def meow(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        meows = [
            "Meow.", "Mreoww!", "Mrrrp?", "Nya!", 
            "*purrrr*", "Meow meow!", "Hiss!", 
            "Feed me right now. I mean, meow.", 
            "Prrbt!", "*stares at you judgingly*"
        ]
        
        embed = discord.Embed(
            title=random.choice(meows),
            color=0x2B2D31,
            timestamp=discord.utils.utcnow()
        )
        
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                # The Cat API provides free random cat images
                async with session.get("https://api.thecatapi.com/v1/images/search") as response:
                    data = await response.json()
                    embed.set_image(url=data[0]["url"])
        except Exception:
            embed.description = "Couldn't find a cat pic, so I guess you just get text. Meow."
            
        embed.set_footer(text="🐾 Meow")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="meowsound", description="Listen to a real cat meow audio clip!")
    async def meowsound(self, interaction: discord.Interaction):
        await interaction.response.defer()

        # True non-repeating random selection
        available = [s for s in CAT_SOUNDS if s not in self.recent_sounds]
        if not available:
            self.recent_sounds.clear()
            available = CAT_SOUNDS

        choice = random.choice(available)
        self.recent_sounds.append(choice)
        if len(self.recent_sounds) > 6:
            self.recent_sounds.pop(0)

        sound_file, sound_title, sound_caption = choice
        file_path = f"assets/sounds/{sound_file}"

        embed = discord.Embed(
            title=f"🔊 {sound_caption}",
            description=(
                f"> 🐱 **Sound:** {sound_title}\n"
                f"> 🎧 Click the player **above** to listen!"
            ),
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(name="🐾 Meow Soundboard", icon_url=interaction.guild.me.display_avatar.url)
        embed.set_footer(text=f"Played for {interaction.user.display_name} • Real Cat Audio", icon_url=interaction.user.display_avatar.url)

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get("https://api.thecatapi.com/v1/images/search") as response:
                    data = await response.json()
                    embed.set_thumbnail(url=data[0]["url"])
        except Exception:
            embed.set_thumbnail(url=interaction.guild.me.display_avatar.url)

        file = discord.File(file_path, filename=sound_file)
        await interaction.followup.send(embed=embed, file=file)

    @app_commands.command(name="tan", description="Check what Tan is currently doing.")
    async def tan(self, interaction: discord.Interaction):
        # True non-repeating random selection
        available = [s for s in TAN_STATUSES if s not in self.recent_tan]
        if not available:
            self.recent_tan.clear()
            available = TAN_STATUSES

        choice = random.choice(available)
        self.recent_tan.append(choice)
        if len(self.recent_tan) > 8:
            self.recent_tan.pop(0)

        icon, status = choice

        embed = discord.Embed(
            title=f"{icon} Tan's Current Status",
            description=f"> **\"{status}\"**",
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(name="🐾 Tan Status Report", icon_url=interaction.guild.me.display_avatar.url)
        embed.set_footer(text=f"Checked by {interaction.user.display_name} • Leave Tan alone rn")
        await interaction.response.send_message(embed=embed)

    CATEGORY_MOODS = {
        "all": [
            ("🎲 Any Vibe", "any"),
            ("🏆 Motivated / Inspired", "motivational"),
            ("🔥 Hyped / Epic", "hyped"),
            ("💖 Romantic / Smooth", "romantic"),
            ("🧠 Deep / Philosophical", "philosophical"),
            ("🥀 Sad / Heartbroken", "sad"),
            ("😂 Funny / Chaotic", "funny"),
        ],
        "motivation": [
            ("🎲 Any Vibe", "any"),
            ("🏆 Motivated / Unstoppable", "motivational"),
            ("🧠 Stoic Wisdom", "philosophical"),
            ("🔥 Hyped / Pure Fire", "hyped"),
        ],
        "old_love": [
            ("🎲 Any Vibe", "any"),
            ("💖 Timeless & Poetic", "romantic"),
            ("🥀 Tragic Romance", "sad"),
            ("🧠 Deep Longing", "philosophical"),
        ],
        "romance": [
            ("🎲 Any Vibe", "any"),
            ("💖 Romantic & Sweet", "romantic"),
            ("🥀 Tearjerker / Sad", "sad"),
        ],
        "rizz": [
            ("🎲 Any Vibe", "any"),
            ("😏 Smooth & Flirty", "romantic"),
            ("😂 Cheesy & Funny", "funny"),
        ],
        "anime": [
            ("🎲 Any Vibe", "any"),
            ("🔥 Hyped / Epic Moments", "hyped"),
            ("🧠 Deep / Philosophical", "philosophical"),
            ("😂 Funny & Chaotic", "funny"),
            ("🥀 Sad / Pain & Heartbreak", "sad"),
        ],
        "cartoons": [
            ("🎲 Any Vibe", "any"),
            ("🧠 Deep & Life Lessons", "philosophical"),
            ("😂 Funny & Chaotic", "funny"),
            ("🏆 Motivated & Inspiring", "motivational"),
            ("🔥 Hyped & Epic", "hyped"),
            ("🥀 Sad Moments", "sad"),
        ],
        "movies": [
            ("🎲 Any Vibe", "any"),
            ("🔥 Hyped & Action Packed", "hyped"),
            ("🧠 Deep & Mind-Blowing", "philosophical"),
            ("😂 Funny & Comedy", "funny"),
        ],
        "books": [
            ("🎲 Any Vibe", "any"),
            ("🧠 Deep & Literary", "philosophical"),
            ("🏆 Inspiring & Courage", "motivational"),
            ("🥀 Melancholy & Loss", "sad"),
            ("🔥 Epic & Bold", "hyped"),
        ],
        "funny": [
            ("🎲 Any Vibe", "any"),
            ("😂 Chaotic Sitcom Comedy", "funny"),
            ("🥀 Broke & Relatable Pain", "sad"),
            ("🧠 Absurd Wisdom", "philosophical"),
        ],
        "other": [
            ("🎲 Any Vibe", "any"),
            ("🧠 Deep & Philosophical", "philosophical"),
            ("🔥 Inspiring Light", "hyped"),
        ],
    }

    @app_commands.command(name="quote", description="Get iconic quotes from anime, cartoons, movies, novels, motivation, romance & more!")
    @app_commands.describe(
        category="Filter quotes by category",
        mood="Filter quotes by mood (customized according to your category!)"
    )
    @app_commands.choices(
        category=[
            app_commands.Choice(name="🎲 Random (Anything)", value="all"),
            app_commands.Choice(name="📜 Server Custom Quotes", value="custom"),
            app_commands.Choice(name="🏆 Motivation & Grind", value="motivation"),
            app_commands.Choice(name="📜 Classic / Old Love", value="old_love"),
            app_commands.Choice(name="📚 Novels & Literature", value="books"),
            app_commands.Choice(name="📺 Cartoons & Animation", value="cartoons"),
            app_commands.Choice(name="🍙 Anime", value="anime"),
            app_commands.Choice(name="🎬 Movies", value="movies"),
            app_commands.Choice(name="💖 Romance", value="romance"),
            app_commands.Choice(name="😏 Rizz & Pickup Lines", value="rizz"),
            app_commands.Choice(name="😂 Funny & Comedy", value="funny"),
            app_commands.Choice(name="🌟 Other & Philosophy", value="other"),
        ]
    )
    async def quote(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str] = None,
        mood: str = None
    ):
        cat_val = category.value if category else "all"

        if cat_val == "custom":
            if not interaction.guild:
                return await interaction.response.send_message("❌ Server custom quotes are only available inside servers!", ephemeral=True)
            await setup_custom_quotes_table()
            async with aiosqlite.connect(DB_PATH) as conn:
                async with conn.execute(
                    "SELECT id, user_id, quote, author, source, category, created_at FROM custom_quotes WHERE guild_id=? ORDER BY RANDOM() LIMIT 1",
                    (interaction.guild.id,)
                ) as cur:
                    row = await cur.fetchone()
            if not row:
                embed = discord.Embed(
                    title="📜 No Custom Quotes Yet!",
                    description=(
                        "This server doesn't have any custom quotes yet!\n\n"
                        "Be the first to add one using `/customquote add`!\n"
                        "Example: `/customquote add quote:I am inevitable author:Thanos source:Endgame`"
                    ),
                    color=0xFEE75C
                )
                return await interaction.response.send_message(embed=embed)

            qid, uid, qtext, qauthor, qsource, qcat, qdate = row
            submitter = interaction.guild.get_member(uid)
            sub_name = submitter.display_name if submitter else f"User {uid}"

            cat_emojis = {
                "funny": "😂",
                "love": "💖",
                "deep": "🧠",
                "out_of_context": "🔥",
                "legendary": "🏆"
            }
            icon = cat_emojis.get(qcat, "📜")

            embed = discord.Embed(
                title=f"{icon} Server Custom Quote #{qid}",
                description=f"### “{qtext}”",
                color=0xF1C40F,
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="👤 Quoted", value=f"**{qauthor}**", inline=True)
            if qsource:
                embed.add_field(name="📍 Context / Source", value=f"*{qsource}*", inline=True)
            embed.set_footer(text=f"Added by {sub_name} • {qdate}")
            return await interaction.response.send_message(embed=embed, view=CustomQuoteView(interaction.guild.id))

        mood_val = mood.lower().strip() if mood else "any"

        # Handle full choice label if passed
        for _, m_val in self.CATEGORY_MOODS["all"]:
            if m_val in mood_val:
                mood_val = m_val
                break

        pool = QUOTES
        if cat_val != "all":
            pool = [q for q in pool if q["category"] == cat_val]
        if mood_val != "any":
            filtered_by_mood = [q for q in pool if q.get("mood") == mood_val]
            if filtered_by_mood:
                pool = filtered_by_mood

        available = [q for q in pool if q["quote"] not in self.recent_quotes]
        if not available:
            self.recent_quotes = [q for q in self.recent_quotes if q not in [x["quote"] for x in pool]]
            available = pool

        picked = random.choice(available)
        self.recent_quotes.append(picked["quote"])
        if len(self.recent_quotes) > 25:
            self.recent_quotes.pop(0)

        cat_colors = {
            "motivation": 0xF39C12,
            "old_love": 0xC2185B,
            "books": 0x9B59B6,
            "cartoons": 0x00CEC9,
            "anime": 0xFF6B81,
            "movies": 0xF1C40F,
            "romance": 0xE91E63,
            "rizz": 0xFF7675,
            "funny": 0x2ECC71,
            "other": 0x5865F2,
            "all": 0x5865F2
        }
        color = cat_colors.get(picked["category"], 0x5865F2)

        cat_names = {
            "motivation": "Motivation & Grind",
            "old_love": "Classic / Old Love",
            "books": "Novels & Literature",
            "cartoons": "Cartoons & Animation",
            "anime": "Anime",
            "movies": "Movies",
            "romance": "Romance",
            "rizz": "Rizz",
            "funny": "Funny & Comedy",
            "other": "Philosophy & Wisdom",
            "all": "All Categories"
        }
        cat_display = cat_names.get(picked["category"], picked["category"].capitalize())

        mood_badges = {
            "motivational": "🏆 Motivated / Inspired",
            "hyped": "🔥 Hyped",
            "sad": "🥀 Sad Boy Hours",
            "funny": "😂 Chaotic Comedy",
            "romantic": "💖 Romantic / Rizz",
            "philosophical": "🧠 Deep Thoughts"
        }
        mood_text = mood_badges.get(picked.get("mood", ""), picked.get("mood", "").capitalize())

        embed = discord.Embed(
            description=f"### “{picked['quote']}”",
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="👤 Character / Speaker", value=f"**{picked['character']}**", inline=True)
        embed.add_field(name="🎬 Work / Franchise", value=f"*{picked['source']}*", inline=True)
        if picked.get("ep"):
            embed.add_field(name="📺 Movie / Ep / Scene", value=f"📍 {picked['ep']}", inline=True)

        embed.set_author(name=f"{picked['icon']} Legendary Quote", icon_url=interaction.guild.me.display_avatar.url)
        embed.set_footer(text=f"Requested by {interaction.user.display_name}")

        await interaction.response.send_message(embed=embed)

    @quote.autocomplete("mood")
    async def quote_mood_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str
    ) -> list[app_commands.Choice[str]]:
        cat = getattr(interaction.namespace, "category", None) or "all"
        mood_list = self.CATEGORY_MOODS.get(cat, self.CATEGORY_MOODS["all"])

        choices = []
        for label, val in mood_list:
            if not current or current.lower() in label.lower() or current.lower() in val.lower():
                choices.append(app_commands.Choice(name=label, value=val))
        return choices[:25]

    # ---------------------------------------------------------
    # CUSTOM QUOTES SYSTEM (Group: /customquote add, get, list, delete)
    # ---------------------------------------------------------
    customquote_group = app_commands.Group(name="customquote", description="Server custom quotes collection & management")

    @customquote_group.command(name="add", description="Add a custom quote to this server's collection!")
    @app_commands.describe(
        quote="The quote text",
        author="Who said it (e.g. @Meoww, Raj, etc.)",
        source="Context or where it was said (e.g. 'Late night VC', 'Minecraft')",
        category="Quote category"
    )
    @app_commands.choices(category=[
        app_commands.Choice(name="😂 Funny & Meme", value="funny"),
        app_commands.Choice(name="💖 Romantic & Cute", value="love"),
        app_commands.Choice(name="🧠 Deep & Wise", value="deep"),
        app_commands.Choice(name="🔥 Out of Context", value="out_of_context"),
        app_commands.Choice(name="🏆 Legendary Moment", value="legendary"),
    ])
    async def customquote_add(
        self,
        interaction: discord.Interaction,
        quote: str,
        author: str,
        source: str = None,
        category: str = "funny"
    ):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Custom quotes can only be added in a server!", ephemeral=True)
        await setup_custom_quotes_table()
        quote = quote.strip()
        author = author.strip()
        source = source.strip() if source else None

        if len(quote) < 2:
            return await interaction.response.send_message("❌ Quote is too short!", ephemeral=True)
        if len(quote) > 1000:
            return await interaction.response.send_message("❌ Quote is too long! Max 1000 characters.", ephemeral=True)

        now = discord.utils.utcnow().strftime("%B %d, %Y")
        async with aiosqlite.connect(DB_PATH) as conn:
            cur = await conn.execute(
                """INSERT INTO custom_quotes (guild_id, user_id, quote, author, source, category, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (interaction.guild.id, interaction.user.id, quote, author, source, category, now)
            )
            await conn.commit()
            quote_id = cur.lastrowid

        embed = discord.Embed(
            title="✅ Custom Quote Added!",
            description=(
                f"### “{quote}”\n\n"
                f"• **Speaker:** **{author}**\n"
                + (f"• **Context / Source:** *{source}*\n" if source else "")
                + f"• **Quote ID:** `#{quote_id}`\n\n"
                f"*Anyone in the server can now view this quote using `/customquote get id:{quote_id}`!*"
            ),
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Added by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @customquote_group.command(name="get", description="Get a server custom quote by ID, member, or random!")
    @app_commands.describe(
        quote_id="Look up a specific quote by ID",
        member="Filter quotes submitted by a specific member"
    )
    async def customquote_get(
        self,
        interaction: discord.Interaction,
        quote_id: int = None,
        member: discord.Member = None
    ):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Custom quotes can only be viewed in a server!", ephemeral=True)
        await setup_custom_quotes_table()
        async with aiosqlite.connect(DB_PATH) as conn:
            if quote_id is not None:
                async with conn.execute(
                    "SELECT id, user_id, quote, author, source, category, created_at FROM custom_quotes WHERE guild_id=? AND id=?",
                    (interaction.guild.id, quote_id)
                ) as cur:
                    row = await cur.fetchone()
                if not row:
                    return await interaction.response.send_message(f"❌ No custom quote found with ID `#{quote_id}`.", ephemeral=True)
            elif member is not None:
                async with conn.execute(
                    "SELECT id, user_id, quote, author, source, category, created_at FROM custom_quotes WHERE guild_id=? AND user_id=? ORDER BY RANDOM() LIMIT 1",
                    (interaction.guild.id, member.id)
                ) as cur:
                    row = await cur.fetchone()
                if not row:
                    return await interaction.response.send_message(f"❌ {member.mention} hasn't submitted any custom quotes yet!", ephemeral=True)
            else:
                async with conn.execute(
                    "SELECT id, user_id, quote, author, source, category, created_at FROM custom_quotes WHERE guild_id=? ORDER BY RANDOM() LIMIT 1",
                    (interaction.guild.id,)
                ) as cur:
                    row = await cur.fetchone()
                if not row:
                    embed = discord.Embed(
                        title="📜 No Custom Quotes Yet!",
                        description=(
                            "This server doesn't have any custom quotes yet!\n\n"
                            "Be the first to add one using `/customquote add`!\n"
                            "Example: `/customquote add quote:I am inevitable author:Thanos source:Endgame`"
                        ),
                        color=0xFEE75C
                    )
                    return await interaction.response.send_message(embed=embed)

        qid, uid, qtext, qauthor, qsource, qcat, qdate = row
        submitter = interaction.guild.get_member(uid)
        sub_name = submitter.display_name if submitter else f"User {uid}"

        cat_emojis = {
            "funny": "😂",
            "love": "💖",
            "deep": "🧠",
            "out_of_context": "🔥",
            "legendary": "🏆"
        }
        icon = cat_emojis.get(qcat, "📜")

        embed = discord.Embed(
            title=f"{icon} Server Custom Quote #{qid}",
            description=f"### “{qtext}”",
            color=0xF1C40F,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="👤 Quoted", value=f"**{qauthor}**", inline=True)
        if qsource:
            embed.add_field(name="📍 Context / Source", value=f"*{qsource}*", inline=True)
        embed.set_footer(text=f"Submitted by {sub_name} • {qdate}")
        await interaction.response.send_message(embed=embed, view=CustomQuoteView(interaction.guild.id))

    @customquote_group.command(name="list", description="List recent custom quotes added to this server.")
    async def customquote_list(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Custom quotes can only be viewed in a server!", ephemeral=True)
        await setup_custom_quotes_table()
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT id, quote, author, created_at FROM custom_quotes WHERE guild_id=? ORDER BY id DESC LIMIT 10",
                (interaction.guild.id,)
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            return await interaction.response.send_message("❌ No custom quotes found in this server. Add some with `/customquote add`!", ephemeral=True)

        embed = discord.Embed(
            title=f"📜 {interaction.guild.name}'s Custom Quotes",
            description="Here are recent quotes added to this server:\n",
            color=0xF1C40F,
            timestamp=discord.utils.utcnow()
        )
        for qid, qtext, qauthor, qdate in rows:
            short_text = qtext[:75] + ("..." if len(qtext) > 75 else "")
            embed.description += f"• **`#{qid}`** “{short_text}” — *{qauthor}* ({qdate})\n"

        embed.set_footer(text="🐾 View any quote with /customquote get [quote_id]")
        await interaction.response.send_message(embed=embed)

    @customquote_group.command(name="delete", description="Delete a custom quote (Submitter, Admin, or Owner only).")
    @app_commands.describe(quote_id="The ID of the quote to delete")
    async def customquote_delete(self, interaction: discord.Interaction, quote_id: int):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Custom quotes can only be deleted in a server!", ephemeral=True)
        await setup_custom_quotes_table()
        is_owner = (interaction.guild and interaction.guild.owner_id == interaction.user.id)
        is_admin = interaction.user.guild_permissions.administrator

        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT user_id, quote FROM custom_quotes WHERE guild_id=? AND id=?",
                (interaction.guild.id, quote_id)
            ) as cur:
                row = await cur.fetchone()

            if not row:
                return await interaction.response.send_message(f"❌ Quote `#{quote_id}` not found.", ephemeral=True)

            creator_id, quote_text = row
            if interaction.user.id != creator_id and not is_admin and not is_owner:
                return await interaction.response.send_message("❌ You can only delete your own quotes, unless you are an Admin!", ephemeral=True)

            await conn.execute("DELETE FROM custom_quotes WHERE guild_id=? AND id=?", (interaction.guild.id, quote_id))
            await conn.commit()

        embed = discord.Embed(
            title="🗑️ Quote Deleted",
            description=f"✅ Successfully deleted quote `#{quote_id}`.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed)


TAN_STATUSES = [
    ("📉", "Losing money in trading, don't disturb rn. Red candles everywhere."),
    ("🤖", "Talking to AI GF, don't disturb rn. She actually understands me."),
    ("💔", "Got rejected again from her... don't disturb rn in bad mood."),
    ("💀", "Looking at portfolio in absolute silence. Do not ping unless you're wiring funds."),
    ("🎮", "Bottom-fragging in Valorant and blaming the wifi. Do not disturb rn."),
    ("📱", "She left me on 'Delivered' 5 hours ago. Let me cope in peace."),
    ("🛌", "Staring at the ceiling questioning all my life decisions. Do not disturb."),
    ("🧠", "Convincing myself that my AI girlfriend loves me for who I am."),
    ("💸", "Bought at the very top, panic sold at the bottom. Financial genius at work."),
    ("🎧", "Listening to sad boy songs on repeat at 3 AM. Do not disturb."),
    ("💔", "She hit me with 'you're such a great friend'... in critical condition rn."),
    ("📊", "The crypto chart formed a head-and-shoulders straight into the abyss. Depressed."),
    ("🧗", "Currently stuck in Iron rank. Hardcore gaming session, don't talk to me."),
    ("🧃", "Drinking juice in the dark staring at her Instagram story. Do not disturb."),
    ("🕯️", "Currently in my villain arc. Don't speak to me unless it's about revenge."),
    ("🍜", "Eating cold 2-minute noodles while staring blankly into the distance."),
    ("🚪", "Locked in the room practicing my apology speech for an argument that hasn't happened yet."),
    ("📈", "Charting random lines on Bitcoin hoping it magically saves my net worth."),
    ("🥶", "Watching her type '...' for 10 minutes and then disappearing. Pain."),
    ("🕶️", "Too cool to be sad, but secretly crying inside. Don't ping rn."),
    ("🛌", "Planning my billionaire comeback arc while lying motionless in bed."),
    ("🥀", "She liked another guy's story. It is completely over for me."),
    ("🔋", "Operating at 1% social battery. Please do not interact."),
    ("📉", "Down 94% on meme coins. Mom thinks I'm an investor."),
    ("🎬", "Practicing eye contact with my reflection so I don't look scared next time she speaks."),
]


QUOTES = [
    # Anime (with Episode & Arc Names)
    {"quote": "Throughout Heaven and Earth, I alone am the honored one. 🤞", "character": "Satoru Gojo", "source": "Jujutsu Kaisen", "ep": "S2 Ep 4 • 'Hidden Inventory, Part 4'", "category": "anime", "mood": "hyped", "icon": "⛩️"},
    {"quote": "Stand proud. You are strong.", "character": "Ryomen Sukuna", "source": "Jujutsu Kaisen", "ep": "S2 Ep 16 • 'Thunderclap, Part 2'", "category": "anime", "mood": "philosophical", "icon": "🔥"},
    {"quote": "Nah, I'd win.", "character": "Satoru Gojo", "source": "Jujutsu Kaisen", "ep": "Ch. 221 • 'Gain and Loss'", "category": "anime", "mood": "hyped", "icon": "✨"},
    {"quote": "Work is shit. Overtime is shit. I'm going to work overtime.", "character": "Kento Nanami", "source": "Jujutsu Kaisen", "ep": "S1 Ep 9 • 'Small Fry and Reverse Retribution'", "category": "anime", "mood": "funny", "icon": "👔"},
    {"quote": "Wake up to reality. Nothing ever goes as planned in this accursed world. The longer you live, the more you realize that the only things that truly exist in this reality are merely pain, suffering and futility.", "character": "Madara Uchiha", "source": "Naruto Shippuden", "ep": "Ep 344 • 'Obito and Madara'", "category": "anime", "mood": "sad", "icon": "🌙"},
    {"quote": "Do you know pain? Accept pain, know pain. Now this world shall know pain... SHINRA TENSEI!", "character": "Pain (Nagato)", "source": "Naruto Shippuden", "ep": "Ep 167 • 'Planetary Devastation'", "category": "anime", "mood": "hyped", "icon": "🪐"},
    {"quote": "People live their lives bound by what they accept as correct and true. That is how they define 'reality'... their 'reality' may all be a mirage.", "character": "Itachi Uchiha", "source": "Naruto Shippuden", "ep": "Ep 135 • 'The Longest Moment'", "category": "anime", "mood": "philosophical", "icon": "👁️"},
    {"quote": "I'll take a potato chip... AND EAT IT! 🥔", "character": "Light Yagami", "source": "Death Note", "ep": "Ep 8 • 'Glare'", "category": "anime", "mood": "funny", "icon": "📓"},
    {"quote": "When do you think people die? When they are shot? No. When they are forgotten!", "character": "Dr. Hiluluk", "source": "One Piece", "ep": "Ep 86 • 'Hiluluk's Cherry Blossoms'", "category": "anime", "mood": "sad", "icon": "🌸"},
    {"quote": "Scars on the back are a swordsman's shame.", "character": "Roronoa Zoro", "source": "One Piece", "ep": "Ep 24 • 'Hawk-Eyes Mihawk'", "category": "anime", "mood": "hyped", "icon": "⚔️"},
    {"quote": "Nothing happened. (standing covered in blood like an absolute legend)", "character": "Roronoa Zoro", "source": "One Piece", "ep": "Ep 377 • 'My Crewmate\'s Pain is My Pain'", "category": "anime", "mood": "hyped", "icon": "🩸"},
    {"quote": "If you don't take risks, you can't create a future.", "character": "Monkey D. Luffy", "source": "One Piece", "ep": "Ep 151 • 'The 100 Million Man'", "category": "anime", "mood": "hyped", "icon": "🍖"},
    {"quote": "IT'S OVER 9000!!! 💥", "character": "Vegeta", "source": "Dragon Ball Z", "ep": "Ep 28 • 'Goku's Arrival'", "category": "anime", "mood": "hyped", "icon": "⚡"},
    {"quote": "I am the hope of the universe. I am the answer to all living things that cry out for peace.", "character": "Son Goku", "source": "Dragon Ball Z", "ep": "Ep 96 • 'Explosion of Anger'", "category": "anime", "mood": "hyped", "icon": "🌟"},
    {"quote": "If you win, you live. If you lose, you die. If you don't fight, you can't win! TATAKAE!", "character": "Eren Yeager", "source": "Attack on Titan", "ep": "S1 Ep 6 • 'The World the Girl Saw'", "category": "anime", "mood": "hyped", "icon": "🕊️"},
    {"quote": "Give up on your dreams and die for us.", "character": "Levi Ackerman", "source": "Attack on Titan", "ep": "S3 Ep 16 • 'Perfect Game'", "category": "anime", "mood": "funny", "icon": "🧹"},
    {"quote": "My soldiers, rage! My soldiers, scream! My soldiers, FIGHT! SHINZO WO SASAGEYO!", "character": "Erwin Smith", "source": "Attack on Titan", "ep": "S3 Ep 16 • 'Perfect Game'", "category": "anime", "mood": "hyped", "icon": "🐎"},
    {"quote": "You thought your first kiss would be JoJo, but it was I, DIO! 🧛‍♂️", "character": "Dio Brando", "source": "JoJo's Bizarre Adventure", "ep": "Part 1, Ep 1 • 'Dio the Invader'", "category": "anime", "mood": "funny", "icon": "🎭"},
    {"quote": "Oh? You're approaching me? Instead of running away, you're coming right to me?", "character": "Dio Brando", "source": "JoJo's Bizarre Adventure", "ep": "Part 3, Ep 46 • 'DIO's World'", "category": "anime", "mood": "hyped", "icon": "🚶‍♂️"},
    {"quote": "Yare yare daze.", "character": "Jotaro Kujo", "source": "JoJo's Bizarre Adventure", "ep": "Part 3, Ep 1 • 'A Man Possessed by an Evil Spirit'", "category": "anime", "mood": "philosophical", "icon": "🧢"},
    {"quote": "100 push-ups, 100 sit-ups, 100 squats, and a 10-kilometer run EVERY SINGLE DAY!", "character": "Saitama", "source": "One Punch Man", "ep": "S1 Ep 3 • 'The Obsessive Scientist'", "category": "anime", "mood": "funny", "icon": "🥊"},
    {"quote": "Since when were you under the impression that I wasn't using Kyoka Suigetsu?", "character": "Sosuke Aizen", "source": "Bleach", "ep": "Ep 293 • 'Hitsugaya, Enraged!'", "category": "anime", "mood": "philosophical", "icon": "👓"},
    {"quote": "Set your heart ablaze. Go beyond your limits!", "character": "Kyojuro Rengoku", "source": "Demon Slayer: Mugen Train", "ep": "Movie • Mugen Train Climax", "category": "anime", "mood": "hyped", "icon": "🔥"},
    {"quote": "If the king doesn't move, then his subjects won't follow. ♟️", "character": "Lelouch vi Britannia", "source": "Code Geass", "ep": "S1 Ep 1 • 'The Day a New Falcon Was Born'", "category": "anime", "mood": "philosophical", "icon": "👑"},

    # Cartoons & Western Animation (with Episode & Arc Names)
    {"quote": "In the darkest times, hope is something you give yourself. That is the meaning of inner strength.", "character": "Uncle Iroh", "source": "Avatar: The Last Airbender", "ep": "Book 2 (Earth), Ep 20 • 'The Crossroads of Destiny'", "category": "cartoons", "mood": "philosophical", "icon": "🍵"},
    {"quote": "Leaves from the vine, falling so slow... Like fragile tiny shells, drifting in the foam. Little soldier boy, come marching home.", "character": "Uncle Iroh", "source": "Avatar: The Last Airbender", "ep": "Book 2 (Earth), Ep 15 • 'The Tales of Ba Sing Se'", "category": "cartoons", "mood": "sad", "icon": "🍂"},
    {"quote": "That's rough, buddy.", "character": "Prince Zuko", "source": "Avatar: The Last Airbender", "ep": "Book 3 (Fire), Ep 14 • 'The Boiling Rock, Part 1'", "category": "cartoons", "mood": "funny", "icon": "🔥"},
    {"quote": "I'm ready, I'm ready, I'm ready!", "character": "SpongeBob SquarePants", "source": "SpongeBob SquarePants", "ep": "S1 Ep 1 • 'Help Wanted'", "category": "cartoons", "mood": "hyped", "icon": "🧽"},
    {"quote": "The inner machinations of my mind are an enigma. *milk carton tips over*", "character": "Patrick Star", "source": "SpongeBob SquarePants", "ep": "S2 Ep 35 • 'The Secret Box'", "category": "cartoons", "mood": "funny", "icon": "🥛"},
    {"quote": "I have no soul. I gave it up years ago.", "character": "Squidward Tentacles", "source": "SpongeBob SquarePants", "ep": "S3 Ep 44 • 'Nasty Patty'", "category": "cartoons", "mood": "funny", "icon": "🎷"},
    {"quote": "Sucking at something is the first step towards being sorta good at something.", "character": "Jake the Dog", "source": "Adventure Time", "ep": "S1 Ep 16 • 'Ocean of Fear'", "category": "cartoons", "mood": "motivational", "icon": "🐕"},
    {"quote": "Wubba Lubba Dub Dub! (In Birdperson's language, it means 'I am in great pain, please help me'.)", "character": "Rick Sanchez", "source": "Rick and Morty", "ep": "S1 Ep 11 • 'Ricksy Business'", "category": "cartoons", "mood": "funny", "icon": "🧪"},
    {"quote": "Nobody exists on purpose. Nobody belongs anywhere. Everybody's gonna die. Come watch TV.", "character": "Morty Smith", "source": "Rick and Morty", "ep": "S1 Ep 8 • 'Rixty Minutes'", "category": "cartoons", "mood": "philosophical", "icon": "📺"},
    {"quote": "It gets easier. Every day it gets a little easier. But you gotta do it every day, that's the hard part. But it does get easier.", "character": "Jogging Baboon", "source": "BoJack Horseman", "ep": "S2 Ep 12 • 'Out to Sea'", "category": "cartoons", "mood": "motivational", "icon": "🏃‍♂️"},
    {"quote": "I am vengeance. I am the night. I am BATMAN!", "character": "Bruce Wayne / Batman", "source": "Batman: The Animated Series", "ep": "S1 Ep 3 • 'Nothing to Fear'", "category": "cartoons", "mood": "hyped", "icon": "🦇"},

    # Movies (with Movie Title & Scene Names)
    {"quote": "Why so serious? Let's put a smile on that face! 🃏", "character": "The Joker", "source": "The Dark Knight (2008)", "ep": "Film (2008) • Mob Summit Scene", "category": "movies", "mood": "funny", "icon": "🃏"},
    {"quote": "You either die a hero, or you live long enough to see yourself become the villain.", "character": "Harvey Dent", "source": "The Dark Knight (2008)", "ep": "Film (2008) • Restaurant Scene", "category": "movies", "mood": "philosophical", "icon": "🦇"},
    {"quote": "Some men aren't looking for anything logical, like money. Some men just want to watch the world burn.", "character": "Alfred Pennyworth", "source": "The Dark Knight (2008)", "ep": "Film (2008) • Batcave Briefing", "category": "movies", "mood": "philosophical", "icon": "🔥"},
    {"quote": "I am inevitable.", "character": "Thanos", "source": "Avengers: Endgame (2019)", "ep": "Film (2019) • Final Battle Climax", "category": "movies", "mood": "hyped", "icon": "🧤"},
    {"quote": "And I... am... Iron Man. 🫰💥", "character": "Tony Stark", "source": "Avengers: Endgame (2019)", "ep": "Film (2019) • The Snap Scene", "category": "movies", "mood": "hyped", "icon": "🦾"},
    {"quote": "I can do this all day.", "character": "Steve Rogers", "source": "Captain America: The First Avenger (2011)", "ep": "Film (2011) • Alleyway Fight Scene", "category": "movies", "mood": "hyped", "icon": "🛡️"},
    {"quote": "No, I am your father.", "character": "Darth Vader", "source": "Star Wars: The Empire Strikes Back (1980)", "ep": "Film (1980) • Cloud City Duel", "category": "movies", "mood": "hyped", "icon": "⚔️"},
    {"quote": "Do or do not. There is no try.", "character": "Yoda", "source": "Star Wars: The Empire Strikes Back (1980)", "ep": "Film (1980) • Dagobah Jedi Training", "category": "movies", "mood": "philosophical", "icon": "🐸"},
    {"quote": "Hello there! ... General Kenobi!", "character": "Obi-Wan Kenobi", "source": "Star Wars: Revenge of the Sith (2005)", "ep": "Film (2005) • Utapau Confrontation", "category": "movies", "mood": "funny", "icon": "✨"},
    {"quote": "It's over Anakin! I have the high ground!", "character": "Obi-Wan Kenobi", "source": "Star Wars: Revenge of the Sith (2005)", "ep": "Film (2005) • Mustafar Duel", "category": "movies", "mood": "hyped", "icon": "🌋"},
    {"quote": "Say 'what' again. Say 'what' again, I dare you, I double dare you, say what one more time!", "character": "Jules Winnfield", "source": "Pulp Fiction (1994)", "ep": "Film (1994) • Apartment Interrogation", "category": "movies", "mood": "funny", "icon": "🍔"},
    {"quote": "With great power comes great responsibility.", "character": "Uncle Ben", "source": "Spider-Man (2002)", "ep": "Film (2002) • Car Conversation", "category": "movies", "mood": "philosophical", "icon": "🕷️"},
    {"quote": "I missed the part where that's my problem.", "character": "Peter Parker (Bully Maguire)", "source": "Spider-Man (2002)", "ep": "Film (2002) • Wrestling Arena", "category": "movies", "mood": "funny", "icon": "🕶️"},
    {"quote": "Pizza time.", "character": "Peter Parker", "source": "Spider-Man 2 (2004)", "ep": "Film (2004) • Opening Delivery", "category": "movies", "mood": "funny", "icon": "🍕"},
    {"quote": "Ogres are like onions. Onions have layers. Ogres have layers!", "character": "Shrek", "source": "Shrek (2001)", "ep": "Film (2001) • Onion Field", "category": "movies", "mood": "funny", "icon": "🧅"},
    {"quote": "WHAT ARE YOU DOING IN MY SWAMP?! 🧅", "character": "Shrek", "source": "Shrek (2001)", "ep": "Film (2001) • Swamp Intrusion", "category": "movies", "mood": "funny", "icon": "🪵"},
    {"quote": "You take the blue pill, the story ends. You take the red pill, you stay in Wonderland and see how deep the rabbit hole goes.", "character": "Morpheus", "source": "The Matrix (1999)", "ep": "Film (1999) • Red Pill Choice", "category": "movies", "mood": "philosophical", "icon": "💊"},
    {"quote": "The first rule of Fight Club is: you do not talk about Fight Club.", "character": "Tyler Durden", "source": "Fight Club (1999)", "ep": "Film (1999) • Basement Speech", "category": "movies", "mood": "philosophical", "icon": "🧼"},
    {"quote": "Are you not entertained?! Is this not why you are here?!", "character": "Maximus", "source": "Gladiator (2000)", "ep": "Film (2000) • Colosseum Battle", "category": "movies", "mood": "hyped", "icon": "⚔️"},
    {"quote": "Hasta la vista, baby. 😎", "character": "The Terminator", "source": "Terminator 2: Judgment Day (1991)", "ep": "Film (1991) • Steel Mill Climax", "category": "movies", "mood": "hyped", "icon": "🦾"},
    {"quote": "Mama always said life was like a box of chocolates. You never know what you're gonna get.", "character": "Forrest Gump", "source": "Forrest Gump (1994)", "ep": "Film (1994) • Bus Stop Bench", "category": "movies", "mood": "philosophical", "icon": "🍫"},

    # Books & Classic Novels (with Chapter & Source Names)
    {"quote": "Not all those who wander are lost.", "character": "J.R.R. Tolkien", "source": "The Fellowship of the Ring", "ep": "Book 1, Chapter 10 • 'Strider'", "category": "books", "mood": "philosophical", "icon": "💍"},
    {"quote": "It does not do to dwell on dreams and forget to live.", "character": "Albus Dumbledore", "source": "Harry Potter and the Sorcerer's Stone", "ep": "Book 1 • Chapter 12 'The Mirror of Erised'", "category": "books", "mood": "philosophical", "icon": "⚡"},
    {"quote": "After all this time? — Always.", "character": "Severus Snape", "source": "Harry Potter and the Deathly Hallows", "ep": "Book 7 • Chapter 33 'The Prince's Tale'", "category": "books", "mood": "sad", "icon": "🦌"},
    {"quote": "Fear is the mind-killer. Fear is the little-death that brings total obliteration.", "character": "Paul Atreides", "source": "Dune by Frank Herbert", "ep": "Novel • Litany Against Fear", "category": "books", "mood": "hyped", "icon": "🏜️"},
    {"quote": "There is some good in this world, Mr. Frodo... and it's worth fighting for.", "character": "Samwise Gamgee", "source": "The Lord of the Rings: The Two Towers", "ep": "Film / Novel • Osgiliath Speech", "category": "books", "mood": "hyped", "icon": "🗡️"},
    {"quote": "War is peace. Freedom is slavery. Ignorance is strength.", "character": "George Orwell", "source": "1984", "ep": "Part 1, Chapter 1 • Ingsoc Slogan", "category": "books", "mood": "philosophical", "icon": "👁️"},
    {"quote": "So we beat on, boats against the current, borne back ceaselessly into the past.", "character": "F. Scott Fitzgerald", "source": "The Great Gatsby", "ep": "Chapter 9 • Closing Line", "category": "books", "mood": "sad", "icon": "🍸"},
    {"quote": "When you have eliminated the impossible, whatever remains, however improbable, must be the truth.", "character": "Sherlock Holmes", "source": "Arthur Conan Doyle", "ep": "The Sign of the Four • Chapter 6", "category": "books", "mood": "philosophical", "icon": "🔍"},
    {"quote": "The mind is its own place, and in itself can make a heaven of hell, a hell of heaven.", "character": "John Milton", "source": "Paradise Lost", "ep": "Book I • Lines 254-255", "category": "books", "mood": "philosophical", "icon": "📖"},
    {"quote": "If you want to know what a man's like, take a good look at how he treats his inferiors, not his equals.", "character": "Sirius Black", "source": "Harry Potter and the Goblet of Fire", "ep": "Book 4 • Chapter 27 'Padfoot Returns'", "category": "books", "mood": "philosophical", "icon": "🐾"},
    {"quote": "All human wisdom is contained in these two words: 'Wait' and 'Hope'.", "character": "Alexandre Dumas", "source": "The Count of Monte Cristo", "ep": "Novel • Final Letter to Maximilien", "category": "books", "mood": "philosophical", "icon": "🗝️"},
    {"quote": "Pain and suffering are always inevitable for a large intelligence and a deep heart.", "character": "Fyodor Dostoevsky", "source": "Crime and Punishment", "ep": "Part 3, Chapter 5", "category": "books", "mood": "sad", "icon": "🪓"},
    {"quote": "The secret of human existence lies not only in living, but in knowing what one lives for.", "character": "Fyodor Dostoevsky", "source": "The Brothers Karamazov", "ep": "Book V, Chapter 5", "category": "books", "mood": "philosophical", "icon": "🕯️"},
    {"quote": "Until I feared I would lose it, I never loved to read. One does not love breathing.", "character": "Scout Finch", "source": "To Kill a Mockingbird", "ep": "Chapter 2 • Scout's Schooling", "category": "books", "mood": "philosophical", "icon": "📖"},
    {"quote": "Real courage is when you know you're licked before you begin, but you begin anyway and see it through no matter what.", "character": "Atticus Finch", "source": "To Kill a Mockingbird", "ep": "Chapter 11 • Atticus's Lesson", "category": "books", "mood": "motivational", "icon": "🦉"},
    {"quote": "Beware; for I am fearless, and therefore powerful.", "character": "The Creature", "source": "Frankenstein by Mary Shelley", "ep": "Chapter 16 • The Creature's Warning", "category": "books", "mood": "hyped", "icon": "⚡"},
    {"quote": "It is a far, far better thing that I do, than I have ever done; it is a far, far better rest that I go to than I have ever known.", "character": "Sydney Carton", "source": "A Tale of Two Cities by Charles Dickens", "ep": "Book 3, Chapter 15 • The Guillotine", "category": "books", "mood": "sad", "icon": "⏳"},
    {"quote": "I am not afraid of storms, for I am learning how to sail my ship.", "character": "Amy March", "source": "Little Women by Louisa May Alcott", "ep": "Chapter 44 • Amy's Voyage", "category": "books", "mood": "motivational", "icon": "⛵"},
    {"quote": "Stay gold, Ponyboy. Stay gold.", "character": "Johnny Cade", "source": "The Outsiders by S.E. Hinton", "ep": "Chapter 9 • Johnny's Dying Words", "category": "books", "mood": "sad", "icon": "🌅"},

    # Classic & Old Love Lines (Poetry & Timeless Literature)
    {"quote": "Whatever our souls are made of, his and mine are the same.", "character": "Catherine Earnshaw", "source": "Wuthering Heights by Emily Brontë", "ep": "Novel • Chapter 9", "category": "old_love", "mood": "romantic", "icon": "🥀"},
    {"quote": "I love you as certain dark things are to be loved, in secret, between the shadow and the soul.", "character": "Pablo Neruda", "source": "100 Love Sonnets", "ep": "Sonnet XVII", "category": "old_love", "mood": "romantic", "icon": "📜"},
    {"quote": "Doubt thou the stars are fire; Doubt that the sun doth move; Doubt truth to be a liar; But never doubt I love.", "character": "William Shakespeare", "source": "Hamlet", "ep": "Act 2, Scene 2 • Letter to Ophelia", "category": "old_love", "mood": "romantic", "icon": "🎭"},
    {"quote": "I have loved you with a love that was more than love.", "character": "Edgar Allan Poe", "source": "Annabel Lee", "ep": "Stanza 2 • Annabel Lee", "category": "old_love", "mood": "romantic", "icon": "🖤"},
    {"quote": "My bounty is as boundless as the sea, my love as deep; the more I give to thee, the more I have, for both are infinite.", "character": "Juliet Capulet", "source": "Romeo and Juliet by Shakespeare", "ep": "Act 2, Scene 2 • The Balcony Scene", "category": "old_love", "mood": "romantic", "icon": "🌹"},
    {"quote": "He stepped down, trying not to look long at her, as if she were the sun, yet he saw her, like the sun, even without looking.", "character": "Leo Tolstoy", "source": "Anna Karenina", "ep": "Part 1, Chapter 9", "category": "old_love", "mood": "romantic", "icon": "☀️"},
    {"quote": "To love or have loved, that is enough. Ask nothing further. There is no other pearl to be found in the dark folds of life.", "character": "Victor Hugo", "source": "Les Misérables", "ep": "Volume 4, Book 5, Chapter 4", "category": "old_love", "mood": "romantic", "icon": "🕊️"},
    {"quote": "Grow old along with me! The best is yet to be.", "character": "Robert Browning", "source": "Rabbi Ben Ezra", "ep": "Stanza 1 • Rabbi Ben Ezra", "category": "old_love", "mood": "romantic", "icon": "⏳"},
    {"quote": "If I loved you less, I might be able to talk about it more.", "character": "Jane Austen", "source": "Emma", "ep": "Volume 3, Chapter 13", "category": "old_love", "mood": "romantic", "icon": "👒"},
    {"quote": "In vain have I struggled. It will not do. My feelings will not be repressed. You must allow me to tell you how ardently I admire and love you.", "character": "Mr. Darcy", "source": "Pride and Prejudice", "ep": "Chapter 34 • First Proposal Scene", "category": "old_love", "mood": "romantic", "icon": "🎩"},
    {"quote": "Yours is the light by which my spirit's born: - you are my sun, my moon, and all my stars.", "character": "E.E. Cummings", "source": "Selected Poems", "ep": "Poem #52 • Selected Works", "category": "old_love", "mood": "romantic", "icon": "✨"},
    {"quote": "I am yours, don't give myself back to me.", "character": "Rumi", "source": "Persian Poetry", "ep": "The Masnavi • Persian Gazal", "category": "old_love", "mood": "romantic", "icon": "🪶"},
    {"quote": "You pierce my soul. I am half agony, half hope... I have loved none but you.", "character": "Captain Frederick Wentworth", "source": "Persuasion by Jane Austen", "ep": "Chapter 23 • Wentworth's Letter", "category": "old_love", "mood": "romantic", "icon": "💌"},
    {"quote": "I would rather share one lifetime with you than face all the ages of this world alone.", "character": "Arwen Undómiel", "source": "The Lord of the Rings: The Fellowship of the Ring", "ep": "Film Adapt. • Bridge Scene", "category": "old_love", "mood": "romantic", "icon": "🧝‍♀️"},
    {"quote": "If you live to be a hundred, I want to live to be a hundred minus one day so I never have to live without you.", "character": "A.A. Milne", "source": "Winnie the Pooh", "ep": "Chapter 3 • Piglet's Conversation", "category": "old_love", "mood": "romantic", "icon": "🍯"},
    {"quote": "You are my heart, my life, my one and only thought.", "character": "Arthur Conan Doyle", "source": "The White Company", "ep": "Historical Novel • Chapter 33", "category": "old_love", "mood": "romantic", "icon": "💌"},

    # Motivation, Grind & Discipline
    {"quote": "You have power over your mind - not outside events. Realize this, and you will find strength.", "character": "Marcus Aurelius", "source": "Meditations", "ep": "Book 4, Section 3", "category": "motivation", "mood": "motivational", "icon": "🏛️"},
    {"quote": "The impediment to action advances action. What stands in the way becomes the way.", "character": "Marcus Aurelius", "source": "Meditations", "ep": "Book 5, Section 20", "category": "motivation", "mood": "motivational", "icon": "🏛️"},
    {"quote": "It is not the critic who counts; not the man who points out how the strong man stumbles... The credit belongs to the man who is actually in the arena.", "character": "Theodore Roosevelt", "source": "Citizenship in a Republic", "ep": "1910 Sorbonne Speech • 'Man in the Arena'", "category": "motivation", "mood": "motivational", "icon": "🏟️"},
    {"quote": "He who has a why to live can bear almost any how.", "character": "Friedrich Nietzsche", "source": "Twilight of the Idols", "ep": "Maxims and Arrows #12", "category": "motivation", "mood": "motivational", "icon": "🧠"},
    {"quote": "Do not pray for an easy life, pray for the strength to endure a difficult one.", "character": "Bruce Lee", "source": "Philosophy of Martial Arts", "ep": "1971 Interview with Pierre Berton", "category": "motivation", "mood": "motivational", "icon": "🥋"},
    {"quote": "The master has failed more times than the beginner has even tried.", "character": "Stephen McCranie", "source": "Doodle Alley", "ep": "Zen Pencils #23", "category": "motivation", "mood": "motivational", "icon": "⚔️"},
    {"quote": "You don't get what you wish for; you get what you work for.", "character": "Kobe Bryant", "source": "Mamba Mentality", "ep": "The Mamba Mentality Playbook", "category": "motivation", "mood": "motivational", "icon": "🐍"},
    {"quote": "Don't count the days, make the days count.", "character": "Muhammad Ali", "source": "Boxing Philosophy", "ep": "1978 Championship Training Interview", "category": "motivation", "mood": "motivational", "icon": "🥊"},
    {"quote": "The man who moves a mountain begins by carrying away small stones.", "character": "Confucius", "source": "Analects", "ep": "The Analects • Chapter 9", "category": "motivation", "mood": "motivational", "icon": "🏔️"},
    {"quote": "If you're going through hell, keep going.", "character": "Winston Churchill", "source": "Historical Speeches", "ep": "1941 Harrow School Address", "category": "motivation", "mood": "motivational", "icon": "🔥"},
    {"quote": "We must all suffer one of two things: the pain of discipline or the pain of regret and disappointment.", "character": "Jim Rohn", "source": "Personal Mastery", "ep": "The Art of Exceptional Living", "category": "motivation", "mood": "motivational", "icon": "⚖️"},
    {"quote": "No citizen has a right to be an amateur in the matter of physical training... what a disgrace it is for a man to grow old without seeing the beauty and strength of which his body is capable.", "character": "Socrates", "source": "Xenophon's Memorabilia", "ep": "Memorabilia • Book III", "category": "motivation", "mood": "motivational", "icon": "🏛️"},
    {"quote": "A smooth sea never made a skilled sailor.", "character": "Franklin D. Roosevelt", "source": "Wisdom Lore", "ep": "Historical Wit & Wisdom", "category": "motivation", "mood": "motivational", "icon": "🌊"},
    {"quote": "The only person you are destined to become is the person you decide to be.", "character": "Ralph Waldo Emerson", "source": "Self-Reliance", "ep": "Self-Reliance (1841)", "category": "motivation", "mood": "motivational", "icon": "🌟"},
    {"quote": "Pain is temporary. Quitting lasts forever.", "character": "Lance Armstrong", "source": "Athletic Lore", "ep": "1999 Tour de France Philosophy", "category": "motivation", "mood": "motivational", "icon": "🚴"},
    {"quote": "Fall seven times, stand up eight. (七転び八起き)", "character": "Japanese Proverb", "source": "Eastern Philosophy", "ep": "Kotowaza Ancient Proverb", "category": "motivation", "mood": "motivational", "icon": "🎋"},
    {"quote": "Discipline equals freedom.", "character": "Jocko Willink", "source": "Discipline Equals Freedom", "ep": "The Field Manual (2017)", "category": "motivation", "mood": "motivational", "icon": "🪖"},
    {"quote": "Hard choices, easy life. Easy choices, hard life.", "character": "Jerzy Gregorek", "source": "The Happy Body", "ep": "Olympic Weightlifting Mentality", "category": "motivation", "mood": "motivational", "icon": "🛡️"},
    {"quote": "I am not what happened to me, I am what I choose to become.", "character": "Carl Jung", "source": "Psychological Reflections", "ep": "Memories, Dreams, Reflections", "category": "motivation", "mood": "motivational", "icon": "🔮"},

    # Modern Romance
    {"quote": "I'd rather die tomorrow than live a hundred years without knowing you.", "character": "John Smith", "source": "Pocahontas (1995)", "ep": "Film (1995) • Prison Tent Scene", "category": "romance", "mood": "romantic", "icon": "🍂"},
    {"quote": "You had me at 'hello'.", "character": "Dorothy Boyd", "source": "Jerry Maguire (1996)", "ep": "Film (1996) • Living Room Reunion", "category": "romance", "mood": "romantic", "icon": "💌"},
    {"quote": "When you realize you want to spend the rest of your life with somebody, you want the rest of your life to start as soon as possible.", "character": "Harry Burns", "source": "When Harry Met Sally (1989)", "ep": "Film (1989) • New Year's Eve Confession", "category": "romance", "mood": "romantic", "icon": "🥂"},
    {"quote": "No matter where in the world you are, I will find you again.", "character": "Taki Tachibana", "source": "Your Name (Kimi no Na wa) (2016)", "ep": "Film (2016) • Kataware-doki Twilight", "category": "romance", "mood": "romantic", "icon": "🌠"},
    {"quote": "Winning that ticket was the best thing that ever happened to me... it brought me to you.", "character": "Jack Dawson", "source": "Titanic (1997)", "ep": "Film (1997) • Bow of the Ship", "category": "romance", "mood": "sad", "icon": "🚢"},
    {"quote": "I want all of you, forever, everyday. You and me... everyday.", "character": "Noah Calhoun", "source": "The Notebook (2004)", "ep": "Film (2004) • The Rain Confrontation", "category": "romance", "mood": "romantic", "icon": "🌧️"},
    {"quote": "If I had a flower for every time I thought of you, I could walk through my garden forever.", "character": "Alfred Lord Tennyson", "source": "Poetry", "ep": "Poem • The Princess (1847)", "category": "romance", "mood": "romantic", "icon": "💐"},
    {"quote": "I swear I couldn't love you more than I do right now, and yet I know I will tomorrow.", "character": "Leo Christopher", "source": "Romance Lore", "ep": "Poetry Anthology", "category": "romance", "mood": "romantic", "icon": "💖"},
    {"quote": "I fell in love the way you fall asleep: slowly, and then all at once.", "character": "Hazel Grace Lancaster", "source": "The Fault in Our Stars (2014)", "ep": "Film / Novel • Hazel's Eulogy", "category": "romance", "mood": "romantic", "icon": "💫"},

    # Rizz & Smooth Pickup Lines
    {"quote": "Are you a magician? Because whenever I look at you, everyone else disappears. 🪄", "character": "Rizz Master", "source": "The Art of Rizz", "ep": "The Magician Opener", "category": "rizz", "mood": "romantic", "icon": "✨"},
    {"quote": "Are you Sukuna's domain? Because being anywhere near you makes my heart experience a sure-hit effect. ⛩️", "character": "Anime Rizzler", "source": "JJK Rizz Collection", "ep": "Shibuya Domain Clash", "category": "rizz", "mood": "romantic", "icon": "🤞"},
    {"quote": "Are you Wi-Fi? Because I'm feeling an undeniable connection with zero latency. 📶", "character": "Tech Rizzler", "source": "Modern Romance", "ep": "Tech Support Flirt", "category": "rizz", "mood": "funny", "icon": "⚡"},
    {"quote": "Are you an overdue library book? Because you've got 'FINE' written all over you. 📚", "character": "Literary Rizzler", "source": "Classic Rizz", "ep": "The Library Fine Special", "category": "rizz", "mood": "funny", "icon": "📖"},
    {"quote": "Do you have a map? Because I just got completely lost in your eyes. 🗺️", "character": "Vintage Rizzler", "source": "Pickup Hall of Fame", "ep": "The Cartographer Approach", "category": "rizz", "mood": "romantic", "icon": "👀"},
    {"quote": "If being breathtaking was a crime, you'd be serving multiple consecutive life sentences without parole. 🚨", "character": "Legal Rizzler", "source": "Federal Rizz Bureau", "ep": "Courtroom Drama Special", "category": "rizz", "mood": "funny", "icon": "⚖️"},
    {"quote": "Is your name Google? Because you have everything I’ve been searching for. 🔍", "character": "Algorithm Rizzler", "source": "Search Engine Love", "ep": "Search Engine Algorithm", "category": "rizz", "mood": "funny", "icon": "💻"},
    {"quote": "My doctor said I'm lacking Vitamin U. Mind writing me a prescription? 💊", "character": "Smooth Criminal", "source": "Clinical Rizz", "ep": "Prescription Refill", "category": "rizz", "mood": "funny", "icon": "🩺"},
    {"quote": "Are you French? Because Eiffel for you instantly. 🥐", "character": "Parisian Rizzler", "source": "European Rizz Tour", "ep": "Parisian Charm Tour", "category": "rizz", "mood": "romantic", "icon": "🗼"},
    {"quote": "I must be in an infinite Tsukuyomi, because there's no way someone as stunning as you is real. 👁️", "character": "Uchiha Rizzler", "source": "Hidden Leaf Rizz", "ep": "Hidden Leaf Genjutsu", "category": "rizz", "mood": "romantic", "icon": "🍥"},
    {"quote": "Are you a keyboard? Because you're definitely my type. ⌨️", "character": "Gamer Rizzler", "source": "Discord Rizz", "ep": "Mechanical Keyboard Switch", "category": "rizz", "mood": "funny", "icon": "🎮"},
    {"quote": "Excuse me, but I think you dropped something: my jaw. 😲", "character": "Shocked Rizzler", "source": "Street Rizz", "ep": "Gravity Drop Technique", "category": "rizz", "mood": "funny", "icon": "💥"},

    # Funny & Sitcoms
    {"quote": "I'm not superstitious, but I am a little stitious.", "character": "Michael Scott", "source": "The Office", "ep": "S4 Ep 2 • 'Fun Run'", "category": "funny", "mood": "funny", "icon": "☕"},
    {"quote": "Sometimes I'll start a sentence and I don't even know where it's going. I just hope I find it along the way.", "character": "Michael Scott", "source": "The Office", "ep": "S5 Ep 12 • 'The Duel'", "category": "funny", "mood": "funny", "icon": "👔"},
    {"quote": "I have no idea what I'm doing, but I know I'm doing it really, really well.", "character": "Andy Dwyer", "source": "Parks and Rec", "ep": "S3 Ep 6 • 'Indianapolis'", "category": "funny", "mood": "funny", "icon": "🎸"},
    {"quote": "There's only one thing I hate more than lying: skim milk. Which is water that's lying about being milk.", "character": "Ron Swanson", "source": "Parks and Rec", "ep": "S5 Ep 17 • 'Partridge'", "category": "funny", "mood": "funny", "icon": "🥩"},
    {"quote": "Cool, cool, cool, cool, cool. No doubt, no doubt, no doubt.", "character": "Jake Peralta", "source": "Brooklyn Nine-Nine", "ep": "S1 Ep 1 • 'Pilot'", "category": "funny", "mood": "funny", "icon": "👮‍♂️"},
    {"quote": "I am once again asking for your financial support.", "character": "Bernie Sanders", "source": "Internet Culture", "ep": "2020 Campaign Meme", "category": "funny", "mood": "funny", "icon": "🧤"},
    {"quote": "I am inevitable. And I am... in credit card debt.", "character": "Millennial Thanos", "source": "Meme Lore", "ep": "Avengers Endgame Meme Edition", "category": "funny", "mood": "sad", "icon": "📉"},
    {"quote": "I was born at a very young age. Later, I grew up to realize adulthood is a scam.", "character": "Anonymous Philosopher", "source": "Internet Wisdom", "ep": "Shower Thoughts Anthology", "category": "funny", "mood": "philosophical", "icon": "🍼"},
    {"quote": "My wallet is like an onion. Opening it makes me cry.", "character": "Broke Legend", "source": "Real Life", "ep": "Grocery Receipt Tragedy", "category": "funny", "mood": "sad", "icon": "🧅"},

    # Other & Philosophy
    {"quote": "In the middle of difficulty lies opportunity.", "character": "Albert Einstein", "source": "Philosophy", "ep": "Theoretical Physics Notes", "category": "other", "mood": "philosophical", "icon": "💡"},
    {"quote": "The only true wisdom is in knowing you know nothing.", "character": "Socrates", "source": "Ancient Philosophy", "ep": "Platonic Dialogues • Apology", "category": "other", "mood": "philosophical", "icon": "🏛️"},
    {"quote": "It is during our darkest moments that we must focus to see the light.", "character": "Aristotle", "source": "Ancient Wisdom", "ep": "Nicomachean Ethics", "category": "other", "mood": "hyped", "icon": "🕯️"},
    {"quote": "Be yourself; everyone else is already taken.", "character": "Oscar Wilde", "source": "Literature", "ep": "The Importance of Being Earnest", "category": "other", "mood": "philosophical", "icon": "✨"},
]


async def setup(bot: commands.Bot):
    await setup_custom_quotes_table()
    await bot.add_cog(Fun(bot))
