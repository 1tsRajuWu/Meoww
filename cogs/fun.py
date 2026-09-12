import discord
from discord import app_commands
from discord.ext import commands
import random
import asyncio

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

class Fun(commands.Cog):
    """Fun and entertainment commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.recent_tan = []
        self.recent_sounds = []
        self.recent_quotes = []

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

    @app_commands.command(name="quote", description="Get iconic & hilarious quotes from anime, movies, and pop culture!")
    @app_commands.describe(category="Filter quotes by category")
    @app_commands.choices(category=[
        app_commands.Choice(name="🎲 Random (Anything)", value="all"),
        app_commands.Choice(name="🍙 Anime Quotes", value="anime"),
        app_commands.Choice(name="🎬 Movie Quotes", value="movie"),
        app_commands.Choice(name="😂 Funny & Sitcoms", value="funny"),
    ])
    async def quote(self, interaction: discord.Interaction, category: app_commands.Choice[str] = None):
        cat_val = category.value if category else "all"

        if cat_val == "all":
            pool = QUOTES
        else:
            pool = [q for q in QUOTES if q["category"] == cat_val]

        available = [q for q in pool if q["quote"] not in self.recent_quotes]
        if not available:
            self.recent_quotes = [q for q in self.recent_quotes if q not in [x["quote"] for x in pool]]
            available = pool

        picked = random.choice(available)
        self.recent_quotes.append(picked["quote"])
        if len(self.recent_quotes) > 15:
            self.recent_quotes.pop(0)

        cat_colors = {
            "anime": 0xFF6B81,
            "movie": 0xF1C40F,
            "funny": 0x2ECC71,
            "all": 0x5865F2
        }
        color = cat_colors.get(picked["category"], 0x5865F2)

        embed = discord.Embed(
            description=f"### “{picked['quote']}”",
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="👤 Character", value=f"**{picked['character']}**", inline=True)
        embed.add_field(name="🎬 Source", value=f"*{picked['source']}*", inline=True)
        embed.set_author(name=f"{picked['icon']} Iconic Quote", icon_url=interaction.guild.me.display_avatar.url)
        embed.set_footer(text=f"Requested by {interaction.user.display_name} • Category: {picked['category'].capitalize()}")

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
    # Anime Quotes
    {"quote": "Throughout Heaven and Earth, I alone am the honored one. 🤞", "character": "Satoru Gojo", "source": "Jujutsu Kaisen", "category": "anime", "icon": "⛩️"},
    {"quote": "Stand proud. You are strong.", "character": "Ryomen Sukuna", "source": "Jujutsu Kaisen", "category": "anime", "icon": "🔥"},
    {"quote": "Nah, I'd win.", "character": "Satoru Gojo", "source": "Jujutsu Kaisen", "category": "anime", "icon": "✨"},
    {"quote": "Work is shit. Overtime is shit. I'm going to work overtime.", "character": "Kento Nanami", "source": "Jujutsu Kaisen", "category": "anime", "icon": "👔"},
    {"quote": "Wake up to reality. Nothing ever goes as planned in this accursed world. The longer you live, the more you realize that the only things that truly exist in this reality are merely pain, suffering and futility.", "character": "Madara Uchiha", "source": "Naruto Shippuden", "category": "anime", "icon": "🌙"},
    {"quote": "Do you know pain? Accept pain, know pain. Now this world shall know pain... SHINRA TENSEI!", "character": "Pain (Nagato)", "source": "Naruto Shippuden", "category": "anime", "icon": "🪐"},
    {"quote": "People live their lives bound by what they accept as correct and true. That is how they define 'reality'... their 'reality' may all be a mirage.", "character": "Itachi Uchiha", "source": "Naruto Shippuden", "category": "anime", "icon": "👁️"},
    {"quote": "I'll take a potato chip... AND EAT IT! 🥔", "character": "Light Yagami", "source": "Death Note", "category": "anime", "icon": "📓"},
    {"quote": "When do you think people die? When they are shot? No. When they are ravaged by an incurable disease? No. It’s when... they are forgotten!", "character": "Dr. Hiluluk", "source": "One Piece", "category": "anime", "icon": "🌸"},
    {"quote": "Scars on the back are a swordsman's shame.", "character": "Roronoa Zoro", "source": "One Piece", "category": "anime", "icon": "⚔️"},
    {"quote": "Nothing happened. (standing completely covered in blood like an absolute legend)", "character": "Roronoa Zoro", "source": "One Piece", "category": "anime", "icon": "🩸"},
    {"quote": "If you don't take risks, you can't create a future.", "character": "Monkey D. Luffy", "source": "One Piece", "category": "anime", "icon": "🍖"},
    {"quote": "IT'S OVER 9000!!! 💥", "character": "Vegeta", "source": "Dragon Ball Z", "category": "anime", "icon": "⚡"},
    {"quote": "I am the hope of the universe. I am the answer to all living things that cry out for peace.", "character": "Son Goku", "source": "Dragon Ball Z", "category": "anime", "icon": "🌟"},
    {"quote": "If you win, you live. If you lose, you die. If you don't fight, you can't win! TATAKAE!", "character": "Eren Yeager", "source": "Attack on Titan", "category": "anime", "icon": "🕊️"},
    {"quote": "Give up on your dreams and die for us.", "character": "Levi Ackerman", "source": "Attack on Titan", "category": "anime", "icon": "🧹"},
    {"quote": "My soldiers, rage! My soldiers, scream! My soldiers, FIGHT! SHINZO WO SASAGEYO!", "character": "Erwin Smith", "source": "Attack on Titan", "category": "anime", "icon": "🐎"},
    {"quote": "You thought your first kiss would be JoJo, but it was I, DIO! 🧛‍♂️", "character": "Dio Brando", "source": "JoJo's Bizarre Adventure", "category": "anime", "icon": "🎭"},
    {"quote": "Oh? You're approaching me? Instead of running away, you're coming right to me?", "character": "Dio Brando", "source": "JoJo's Bizarre Adventure", "category": "anime", "icon": "🚶‍♂️"},
    {"quote": "Yare yare daze.", "character": "Jotaro Kujo", "source": "JoJo's Bizarre Adventure", "category": "anime", "icon": "🧢"},
    {"quote": "100 push-ups, 100 sit-ups, 100 squats, and a 10-kilometer run EVERY SINGLE DAY!", "character": "Saitama", "source": "One Punch Man", "category": "anime", "icon": "🥊"},
    {"quote": "Since when were you under the impression that I wasn't using Kyoka Suigetsu?", "character": "Sosuke Aizen", "source": "Bleach", "category": "anime", "icon": "👓"},
    {"quote": "Set your heart ablaze. Go beyond your limits!", "character": "Kyojuro Rengoku", "source": "Demon Slayer", "category": "anime", "icon": "🔥"},
    {"quote": "If the king doesn't move, then his subjects won't follow. ♟️", "character": "Lelouch vi Britannia", "source": "Code Geass", "category": "anime", "icon": "👑"},
    {"quote": "The world isn't perfect. But it's there for us, doing the best it can... that's what makes it so damn beautiful.", "character": "Roy Mustang", "source": "Fullmetal Alchemist", "category": "anime", "icon": "🔥"},

    # Movie Quotes
    {"quote": "Why so serious? Let's put a smile on that face! 🃏", "character": "The Joker", "source": "The Dark Knight", "category": "movie", "icon": "🃏"},
    {"quote": "You either die a hero, or you live long enough to see yourself become the villain.", "character": "Harvey Dent", "source": "The Dark Knight", "category": "movie", "icon": "🦇"},
    {"quote": "Some men aren't looking for anything logical, like money. Some men just want to watch the world burn.", "character": "Alfred Pennyworth", "source": "The Dark Knight", "category": "movie", "icon": "🔥"},
    {"quote": "I am inevitable.", "character": "Thanos", "source": "Avengers: Endgame", "category": "movie", "icon": "🧤"},
    {"quote": "And I... am... Iron Man. 🫰💥", "character": "Tony Stark", "source": "Avengers: Endgame", "category": "movie", "icon": "🦾"},
    {"quote": "I can do this all day.", "character": "Steve Rogers", "source": "Captain America", "category": "movie", "icon": "🛡️"},
    {"quote": "No, I am your father.", "character": "Darth Vader", "source": "Star Wars: The Empire Strikes Back", "category": "movie", "icon": "⚔️"},
    {"quote": "Do or do not. There is no try.", "character": "Yoda", "source": "Star Wars: The Empire Strikes Back", "category": "movie", "icon": "🐸"},
    {"quote": "Hello there! ... General Kenobi!", "character": "Obi-Wan Kenobi", "source": "Star Wars: Revenge of the Sith", "category": "movie", "icon": "✨"},
    {"quote": "It's over Anakin! I have the high ground!", "character": "Obi-Wan Kenobi", "source": "Star Wars: Revenge of the Sith", "category": "movie", "icon": "🌋"},
    {"quote": "Say 'what' again. Say 'what' again, I dare you, I double dare you, say what one more time!", "character": "Jules Winnfield", "source": "Pulp Fiction", "category": "movie", "icon": "🍔"},
    {"quote": "With great power comes great responsibility.", "character": "Uncle Ben", "source": "Spider-Man", "category": "movie", "icon": "🕷️"},
    {"quote": "I missed the part where that's my problem.", "character": "Peter Parker (Bully Maguire)", "source": "Spider-Man", "category": "movie", "icon": "🕶️"},
    {"quote": "Pizza time.", "character": "Peter Parker", "source": "Spider-Man 2", "category": "movie", "icon": "🍕"},
    {"quote": "Ogres are like onions. Onions have layers. Ogres have layers!", "character": "Shrek", "source": "Shrek", "category": "movie", "icon": "🧅"},
    {"quote": "WHAT ARE YOU DOING IN MY SWAMP?! 🧅", "character": "Shrek", "source": "Shrek", "category": "movie", "icon": "🪵"},
    {"quote": "You take the blue pill, the story ends. You take the red pill, you stay in Wonderland, and I show you how deep the rabbit hole goes.", "character": "Morpheus", "source": "The Matrix", "category": "movie", "icon": "💊"},
    {"quote": "The first rule of Fight Club is: you do not talk about Fight Club.", "character": "Tyler Durden", "source": "Fight Club", "category": "movie", "icon": "🧼"},
    {"quote": "I'm gonna make him an offer he can't refuse.", "character": "Vito Corleone", "source": "The Godfather", "category": "movie", "icon": "🌹"},
    {"quote": "Are you not entertained?! Is this not why you are here?!", "character": "Maximus", "source": "Gladiator", "category": "movie", "icon": "⚔️"},
    {"quote": "One does not simply walk into Mordor.", "character": "Boromir", "source": "The Lord of the Rings", "category": "movie", "icon": "🌋"},
    {"quote": "YOU SHALL NOT PASS! 🧙‍♂️", "character": "Gandalf", "source": "The Lord of the Rings", "category": "movie", "icon": "🪄"},
    {"quote": "My precious! 💍", "character": "Gollum", "source": "The Lord of the Rings", "category": "movie", "icon": "🐟"},
    {"quote": "Hasta la vista, baby. 😎", "character": "The Terminator", "source": "Terminator 2: Judgment Day", "category": "movie", "icon": "🦾"},
    {"quote": "Say hello to my little friend! 💥", "character": "Tony Montana", "source": "Scarface", "category": "movie", "icon": "🔫"},
    {"quote": "Mama always said life was like a box of chocolates. You never know what you're gonna get.", "character": "Forrest Gump", "source": "Forrest Gump", "category": "movie", "icon": "🍫"},

    # Funny / Sitcom / Internet Culture
    {"quote": "I'm not superstitious, but I am a little stitious.", "character": "Michael Scott", "source": "The Office", "category": "funny", "icon": "☕"},
    {"quote": "Sometimes I'll start a sentence and I don't even know where it's going. I just hope I find it along the way.", "character": "Michael Scott", "source": "The Office", "category": "funny", "icon": "👔"},
    {"quote": "I am running away from my responsibilities. And it feels good.", "character": "Michael Scott", "source": "The Office", "category": "funny", "icon": "🏃‍♂️"},
    {"quote": "I have no idea what I'm doing, but I know I'm doing it really, really well.", "character": "Andy Dwyer", "source": "Parks and Recreation", "category": "funny", "icon": "🎸"},
    {"quote": "There's only one thing I hate more than lying: skim milk. Which is water that's lying about being milk.", "character": "Ron Swanson", "source": "Parks and Recreation", "category": "funny", "icon": "🥩"},
    {"quote": "I am once again asking for your financial support.", "character": "Bernie Sanders", "source": "Internet Culture", "category": "funny", "icon": "🧤"},
    {"quote": "Cool, cool, cool, cool, cool. No doubt, no doubt, no doubt.", "character": "Jake Peralta", "source": "Brooklyn Nine-Nine", "category": "funny", "icon": "👮‍♂️"},
    {"quote": "I was born at a very young age. Later, I grew up to realize adulthood is a scam.", "character": "Anonymous Philosopher", "source": "Internet Wisdom", "category": "funny", "icon": "🍼"},
    {"quote": "I am inevitable. And I am... in credit card debt.", "character": "Millennial Thanos", "source": "Meme Lore", "category": "funny", "icon": "📉"},
]


async def setup(bot: commands.Bot):
    await bot.add_cog(Fun(bot))
