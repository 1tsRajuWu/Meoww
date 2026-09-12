import discord
from discord import app_commands
from discord.ext import commands
import random
import asyncio

KMS_RESPONSES = [
    ("🩸", "I just killed myself. Blood is literally EVERYWHERE. The walls, the floor, the ceiling fan is just redistributing it at this point."),
    ("💀", "I have killed myself. My body is on the floor. Someone please step over it carefully, I just mopped."),
    ("🩸", "Just ended it all. Blood is dripping down the walls like a horror movie. The vibes in here are IMMACULATE."),
    ("☠️", "I stabbed myself 47 times and I feel GREAT about it. Blood pool forming. Very aesthetic. Very dark academia."),
    ("🩸", "I killed myself and my ghost is still here because there's no wifi in the afterlife and I'm NOT leaving without my playlist."),
    ("💀", "I have perished. My corpse is on the floor. The blood has reached the neighbour's apartment. They knocked. I can't answer. I'm dead."),
    ("🩸", "Killed myself. Blood everywhere. Mom's gonna be so mad. Worth it."),
    ("☠️", "I ended my life and my last thought was 'did I leave the oven on'. I did. The house is also on fire now. Thriving."),
    ("💀", "Just killed myself. The blood splatter is honestly kind of beautiful. CSI would have a field day. Someone call them, I want the attention."),
    ("🩸", "I have committed self-deletion. My body is decorating the floor. 10/10 interior design choice. Very red. Very bold."),
    ("☠️", "Killed myself. Blood is seeping under the door. The neighbours are concerned. I cannot comfort them. I am deceased."),
    ("💀", "I died. I killed myself and I died. Blood is absolutely everywhere. It's on the dog. The dog looks confused but make no mistake, this is her fault."),
    ("🩸", "I jumped. It did not work out. There is blood on things that should not have blood on them. This is fine."),
    ("☠️", "I am dead. I killed myself. The blood has formed a shape on the floor. It looks like a WiFi symbol. Even in death I crave connection."),
    ("💀", "I have slain myself. The crime scene investigators will find 3 energy drink cans and a Discord tab open. They will understand."),
    ("🩸", "Killed myself. Left a note. The note just says 'ratio'. Responders are confused. They should be."),
    ("☠️", "I ended it. Blood is still pooling. It's been 4 minutes. There's a LOT of blood. Someone please tell my plants I'm sorry."),
    ("💀", "I have self-terminated. My ghost watched the paramedics arrive and honestly their reaction was valid."),
    ("🩸", "I killed myself and wrote 'lol' in blood on the wall. I want my funeral to have the same energy."),
    ("☠️", "Self-destruction complete. Blood is on the ceiling somehow. I don't know how. I'm impressed with myself even in death."),
    ("💀", "I perished. The blood is everywhere. It's on the keyboard. My last message to the world is now: `asdfjkl;`. Deep."),
    ("🩸", "I killed myself dramatically. Slow motion fall. Hair flowing. Blood arcing perfectly. It was cinematic. 9/10."),
    ("☠️", "I have ceased to exist. My bloodstain is shaped like a W. I won even in death."),
    ("💀", "Killed myself. The blood ruined my hoodie. I'm somehow more upset about the hoodie."),
    ("🩸", "I ended my life. My ghost immediately came back to check my notifs. 14 pings. This is why I did it."),
]

OPENINGS = [
    "I just killed myself.",
    "I have self-deleted.",
    "I just ended my whole existence.",
    "I died. I killed myself and I died.",
    "I have slain myself dramatically.",
    "Just committed unalive.",
    "I took the final exit.",
    "Self-destruction protocol executed.",
    "I have ceased all bodily operations.",
    "I pulled the plug on my own life.",
    "I went into the light. It was blinding.",
    "I have departed this mortal realm.",
    "I threw myself off the deep end.",
    "I just flatlined on purpose.",
]

BLOOD_SITUATIONS = [
    "Blood is literally EVERYWHERE. The walls, the floor, the ceiling fan is just redistributing it.",
    "Blood is dripping down the walls like a cheap horror movie set.",
    "There's a pool of blood so deep someone called the Coast Guard.",
    "Blood is on the keyboard, the monitor, and somehow inside the microwave.",
    "The crime scene looks like a paint factory explosion, but red.",
    "Blood splatter analyzed: 100% pure unfiltered drama.",
    "Blood is seeping under the door into the hallway. The neighbours are panicking.",
    "The room is drenched in blood. The aesthetic is terrifyingly vibrant.",
    "Blood has painted a masterpiece on the wall that belongs in the Louvre.",
    "Blood is flowing like a waterfall. Even Dracula would say this is too much.",
]

ABSURD_DETAILS = [
    "Someone please step over my corpse carefully, I literally just vacuumed.",
    "Mom is going to be so furious about the white carpet.",
    "The dog looked at my body, sighed, and went back to sleep.",
    "My last words were literally 'watch this, it'll be funny'.",
    "Left a suicide note on the fridge that just says 'skill issue'.",
    "Paramedics arrived and immediately started rating my fall 7/10.",
    "My ghost watched the whole thing and honestly I'm disappointed in my form.",
    "The blood ruined my limited-edition hoodie. Honestly more mad about that.",
    "CSI showed up with crime tape and I'm posing my corpse for the cameras.",
    "The coroner opened my tab and saw 54 unread Discord pings. He understood.",
    "My ghost is currently haunting this exact channel. You're never getting rid of me.",
]

CLOSERS = [
    "Goodbye cruel world. 💀",
    "Tell my wifi router I loved it. 📶",
    "10/10 death scene, would perish again. 🎬",
    "Press F to pay respects. 🕯️",
    "My tombstone will read: 'He died as he lived: an idiot'. 🪦",
    "Do not disturb, currently rotting. ☠️",
    "Crying, screaming, throwing up, dying. Literally. 🩸",
    "RIP me. Best to ever do it. 🌟",
    "Gone but never forgotten (please forget me). 🖤",
]

def get_kms_response():
    # 30% chance of classic curated quote, 70% chance of dynamic modular generation (10,000+ possibilities)
    if random.random() < 0.3:
        return random.choice(KMS_RESPONSES)
    
    icon = random.choice(["🩸", "💀", "☠️", "🪦", "😵"])
    opening = random.choice(OPENINGS)
    blood = random.choice(BLOOD_SITUATIONS)
    detail = random.choice(ABSURD_DETAILS)
    closer = random.choice(CLOSERS)
    
    return icon, f"{opening} {blood} {detail} {closer}"

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


async def setup(bot: commands.Bot):
    await bot.add_cog(Fun(bot))
