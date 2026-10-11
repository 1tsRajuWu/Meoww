import discord
from discord import app_commands
from discord.ext import commands
import random
import asyncio
import aiosqlite
from utils import db, gifs
from cogs.economy import get_eco, update_wallet, ensure_eco, take_stake
from utils.style import send_card, display_name, pings

DB_PATH = db.DB_PATH

# -------------------------------------------------------------
# ANIMAL DATA (Tiers: Common, Uncommon, Rare, Epic, Mythic, Legendary)
# -------------------------------------------------------------
ANIMALS = {
    "common": [
        ("🐭", "Mouse", 50),
        ("🐸", "Frog", 50),
        ("🐥", "Baby Chick", 50),
        ("🐹", "Hamster", 50),
        ("🐰", "Bunny", 50),
        ("🦇", "Bat", 50),
        ("🐿️", "Chipmunk", 50),
        ("🐦", "Sparrow", 50),
        ("🐌", "Snail", 50),
        ("🐛", "Caterpillar", 50),
    ],
    "uncommon": [
        ("🦊", "Red Fox", 150),
        ("🐺", "Dire Wolf", 150),
        ("🐗", "Wild Boar", 150),
        ("🦝", "Raccoon", 150),
        ("🐒", "Monkey", 150),
        ("🦨", "Skunk", 150),
        ("🦔", "Hedgehog", 150),
        ("🦉", "Barn Owl", 150),
        ("🦆", "Mallard Duck", 150),
        ("🦦", "River Otter", 150),
    ],
    "rare": [
        ("🐼", "Giant Panda", 500),
        ("🐯", "Bengal Tiger", 500),
        ("🦁", "King Lion", 500),
        ("🐻", "Grizzly Bear", 500),
        ("🐬", "Dolphin", 500),
        ("🦈", "Mako Shark", 500),
        ("🐊", "Alligator", 500),
        ("🦚", "Royal Peacock", 500),
        ("🦅", "Bald Eagle", 500),
        ("🐆", "Snow Leopard", 500),
    ],
    "epic": [
        ("🦄", "Mystic Unicorn", 2_000),
        ("🦅", "Fire Phoenix", 2_000),
        ("🐙", "Deepsea Kraken", 2_000),
        ("🦕", "Brachiosaurus", 2_000),
        ("🐉", "Emerald Wyvern", 2_000),
        ("🦭", "Celestial Seal", 2_000),
        ("🦩", "Solar Flamingo", 2_000),
    ],
    "mythic": [
        ("🐲", "Ancient Dragon", 10_000),
        ("👑", "Golden Griffin", 10_000),
        ("🌌", "Celestial Fox", 10_000),
        ("⚡", "Thunder Bird", 10_000),
        ("🌋", "Infernal Behemoth", 10_000),
    ],
    "legendary": [
        ("✨", "Cosmic Kitty", 50_000),
        ("💫", "Eternal Leviathan", 50_000),
        ("🌟", "God Cat", 50_000),
        ("🔮", "Void Phantom", 50_000),
    ],
}

# /hunt hands out free coins and animals, so it needs a cooldown or it can be
# spammed indefinitely.
HUNT_COOLDOWN_SECONDS = 30 * 60

TIER_COLORS = {
    "common": 0x95A5A6,
    "uncommon": 0x2ECC71,
    "rare": 0x3498DB,
    "epic": 0x9B59B6,
    "mythic": 0xF1C40F,
    "legendary": 0xE91E63,
}

TIER_NAMES = {
    "common": "⚪ Common",
    "uncommon": "🟢 Uncommon",
    "rare": "🔵 Rare",
    "epic": "🟣 Epic",
    "mythic": "🟡 Mythic",
    "legendary": "💎 Legendary",
}

# -------------------------------------------------------------
# ANIME ACTION CAPTIONS (and what each action looks like)
# -------------------------------------------------------------
# The pictures are deliberately **not** here. Every entry used to carry a list of
# hand-typed ``media.tenor.com`` links, and every one of them answered 404 — which
# the bot cannot notice and Discord draws as an empty box, the "no gif" report.
# ``utils/gifs`` resolves and verifies an image at the moment it is used instead,
# so a deleted file is a missing picture rather than a broken one.
ACTIONS_DATA = {
    "bonk": {
        "title": "🔨 BONK!",
        "color": 0xFEE75C,
        "lines": [
            "{author} pulled out a 100-ton squeaky cartoon hammer and **BONKED** {target}! 💥",
            "*BONK!* Go directly to horny jail, {target}! Do not pass GO! 🚨",
            "{author} delivered a devastating tactical bonk straight to {target}'s forehead! 🔨",
            "{target} got bonked into the next dimension by {author}! Ouch! 💫",
        ]
    },
    "slap": {
        "title": "👋 SLAP!",
        "color": 0xED4245,
        "lines": [
            "{author} slapped some sense into {target}! 👋",
            "The echo of {author}'s slap against {target}'s cheek resonated across the whole server! 💥",
            "{author} delivered a 5-star slap across {target}'s face! ✨",
        ]
    },
    "hug": {
        "title": "🤗 Warm Hug!",
        "color": 0x57F287,
        "lines": [
            "{author} wrapped their arms around {target} in a super warm and cozy hug! ❤️",
            "{author} glomped {target} tightly and refuses to let go! 🥰",
            "{author} gave {target} the biggest, warmest embrace ever! 🌸",
        ]
    },
    "kiss": {
        "title": "💋 Sweet Kiss!",
        "color": 0xEB459E,
        "lines": [
            "{author} leaned in and gave {target} a sweet kiss on the cheek! 🌸",
            "*smooch* {author} kissed {target}! Look at them blushing red! 🥰",
            "{author} gave {target} a tender, romantic kiss! ✨",
        ]
    },
    "pat": {
        "title": "🐾 Headpats!",
        "color": 0x5865F2,
        "lines": [
            "{author} gently patted {target} on the head. Good boy/girl! 🐾",
            "{author} gave {target} soft, affectionate headpats! *purrrrr* 🐱",
            "{author} strokes {target}'s hair tenderly. There, there! 💫",
        ]
    },
    "punch": {
        "title": "🥊 K.O. Punch!",
        "color": 0xE74C3C,
        "lines": [
            "{author} hit {target} with an anime knockout punch! Critical hit! 💥",
            "ONE PUNCH! {author} sent {target} flying into orbit! 🚀",
            "{author} socked {target} right in the jaw! K.O.! 🥊",
        ]
    },
    "bite": {
        "title": "🦷 Playful Chomp!",
        "color": 0xE67E22,
        "lines": [
            "{author} took a playful little chomp out of {target}! *nom nom* 😋",
            "{target} got bitten by {author}! Ow, watch the teeth! 😼",
            "{author} gnaws on {target}'s ear affectionately! 🐾",
        ]
    },
    "cuddle": {
        "title": "🧸 Cozy Cuddle!",
        "color": 0x9B59B6,
        "lines": [
            "{author} snuggled up and cuddled with {target} under a fluffy blanket! 🧸",
            "{author} pulled {target} close for the coziest snuggle ever! 💖",
        ]
    },
    "poke": {
        "title": "👉 Boop / Poke!",
        "color": 0x1ABC9C,
        "lines": [
            "{author} poked {target}'s squishy cheek repeatedly! *poke poke* 👉",
            "{author} booped {target} right on the nose! Boop! 🐽",
        ]
    },
    "feed": {
        "title": "🍰 Feeding Time!",
        "color": 0xF39C12,
        "lines": [
            "{author} feeds {target} a delicious sweet treat! Say 'aaahhh~' 🍓🍰",
            "{author} fed {target} yummy snacks! *nom nom nom* 🍪",
        ]
    },
    "blush": {
        "title": "😊 Shy Blush!",
        "color": 0xFFB6C1,
        "lines": [
            "{author} made {target} blush bright red! Look awayyy! 😊",
            "{target} is blushing furiously because of {author}! How cute! 🌸",
        ]
    },
    "smile": {
        "title": "😄 Happy Smile!",
        "color": 0xF9E04B,
        "lines": [
            "{author} put the biggest smile on {target}'s face! 😄",
            "{author} and {target} are grinning ear to ear together! ✨",
        ]
    },
    "wave": {
        "title": "👋 Friendly Wave!",
        "color": 0x1ABC9C,
        "lines": [
            "{author} waved enthusiastically at {target}! Hiiii! 👋",
            "{author} spotted {target} and waved like they haven't seen them in years! 🌊",
        ]
    },
    "wink": {
        "title": "😉 Sly Wink!",
        "color": 0x9B59B6,
        "lines": [
            "{author} shot {target} a sly little wink~ You know what that means 😉",
            "{author} winked at {target} with maximum smoothness! 😎",
        ]
    },
    "dance": {
        "title": "💃 Dance Party!",
        "color": 0xE91E63,
        "lines": [
            "{author} grabbed {target} for an impromptu dance party! 💃🕺",
            "{author} and {target} are tearing up the dance floor together! 🎶",
            "DJ dropped the beat and {author} dragged {target} into the spotlight! ✨",
        ]
    },
    "laugh": {
        "title": "😂 Shared Laugh!",
        "color": 0xF1C40F,
        "lines": [
            "{author} burst out laughing with {target}! Absolutely hilarious! 😂",
            "{author} and {target} are wheezing with laughter together! 🤣",
        ]
    },
    "cry": {
        "title": "😭 Shoulder To Cry On!",
        "color": 0x5DADE2,
        "lines": [
            "{author} started ugly-crying on {target}'s shoulder... there, there 😭",
            "{target} is comforting {author} through the waterworks! 🤗",
        ]
    },
    "lick": {
        "title": "👅 Surprise Lick!",
        "color": 0xFF7675,
        "lines": [
            "{author} gave {target} a surprise lick?! Blehaha, what was that for! 👅",
            "{target} just got licked by {author}! Someone's feeling mischievous today! 😜",
        ]
    },
    "tickle": {
        "title": "🪶 Tickle Attack!",
        "color": 0xFDCB6E,
        "lines": [
            "{author} launched a relentless tickle attack on {target}! Stoooop! 🤣",
            "{target} is squirming under {author}'s tickle torture! No mercy! 🪶",
        ]
    }
}

# -------------------------------------------------------------
# CRASH GAME VIEW (Interactive Rocket Multiplier)
# -------------------------------------------------------------
class CrashView(discord.ui.View):
    def __init__(self, user_id: int, guild_id: int, bet: int, crash_point: float):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.guild_id = guild_id
        self.bet = bet
        self.crash_point = crash_point
        self.cashed_out = False
        self.multiplier = 1.00
        self.ended = False

    @discord.ui.button(label="🛑 Cash Out (+0 🪙)", style=discord.ButtonStyle.success)
    async def cash_out_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This is not your rocket game!", ephemeral=True)
        if self.ended or self.cashed_out:
            return await interaction.response.send_message("❌ Game has already ended.", ephemeral=True)

        self.cashed_out = True
        self.ended = True
        winnings = int(self.bet * self.multiplier)
        await update_wallet(self.user_id, self.guild_id, winnings)

        self.clear_items()
        embed = discord.Embed(
            title="🚀 CRASH — Cashed Out!",
            description=(
                f"🎉 **Spectacular! You cashed out in time!**\n\n"
                f"• **Exit Multiplier:** `🚀 {self.multiplier:.2f}x`\n"
                f"• **Initial Bet:** `{self.bet:,}` 🪙\n"
                f"• **Total Payout:** `+{winnings:,}` 🪙\n"
                f"• **Net Profit:** `+{winnings - self.bet:,}` 🪙"
            ),
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Played by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        if not self.ended:
            self.ended = True
            self.clear_items()


# -------------------------------------------------------------
# MARRIAGE VIEW (Accept / Decline Proposal)
# -------------------------------------------------------------
COUPLE_ROLE_COLOUR = 0xFFB6C1  # soft pink
COUPLE_SEPARATOR = " x "
COUPLE_MAX_PART = 40
# Kept for migration: previous shared-role version.
MARRIED_ROLE_NAME = "💍 Married"


def _clean_couple_part(name: str | None, fallback: str) -> str:
    cleaned = (name or "").replace("\n", " ").replace("\r", " ").strip()
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        cleaned = fallback
    if len(cleaned) > COUPLE_MAX_PART:
        cleaned = cleaned[:COUPLE_MAX_PART].rstrip() or fallback
    return cleaned


def _couple_role_name(mem_a, mem_b) -> str:
    return _pair_role_name(mem_a, mem_b, COUPLE_SEPARATOR)


def _pair_role_name(mem_a, mem_b, sep: str) -> str:
    first, second = (mem_a, mem_b) if mem_a.id < mem_b.id else (mem_b, mem_a)
    n1 = _clean_couple_part(
        getattr(first, "display_name", None) or getattr(first, "name", None),
        f"User{first.id % 10000}",
    )
    n2 = _clean_couple_part(
        getattr(second, "display_name", None) or getattr(second, "name", None),
        f"User{second.id % 10000}",
    )
    return f"{n1}{sep}{n2}"[:100]


async def _resolve_member(guild: discord.Guild, user_id: int):
    m = guild.get_member(user_id)
    if m is not None:
        return m
    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.HTTPException):
        return None


async def _move_role_to_top(guild: discord.Guild, role) -> None:
    try:
        me = guild.me
        top = getattr(me, "top_role", None) if me is not None else None
        if top is None or role is None:
            return
        if role.position >= top.position:
            return
        await role.edit(position=top.position - 1, reason="Couple role first")
    except (discord.Forbidden, discord.HTTPException):
        return
    except Exception:
        return


async def _get_or_create_couple_role(guild: discord.Guild | None, mem_a, mem_b):
    return await _get_or_create_pair_role(
        guild, mem_a, mem_b,
        sep=COUPLE_SEPARATOR, colour=COUPLE_ROLE_COLOUR, noun="Couple",
    )


async def _get_or_create_pair_role(guild: discord.Guild | None, mem_a, mem_b, *, sep: str, colour: int, noun: str):
    if guild is None or mem_a is None or mem_b is None:
        return None
    base = _pair_role_name(mem_a, mem_b, sep)
    name = base
    for attempt in range(1, 10):
        role = discord.utils.get(guild.roles, name=name)
        if role is None:
            break
        try:
            member_ids = {m.id for m in role.members}
        except Exception:
            member_ids = set()
        if not member_ids or member_ids <= {mem_a.id, mem_b.id}:
            await _move_role_to_top(guild, role)
            return role
        # Name taken by a different couple — make it unique.
        name = f"{base} {attempt + 1}"[:100]
    else:
        return discord.utils.get(guild.roles, name=name)
    try:
        role = await guild.create_role(
            name=name,
            colour=discord.Colour(colour),
            hoist=True,
            mentionable=True,
            reason=f"{noun} role for {base}",
        )
    except (discord.Forbidden, discord.HTTPException):
        return None
    await _move_role_to_top(guild, role)
    return role


async def _add_couple_role(guild: discord.Guild | None, id1: int, id2: int) -> str:
    return await _add_pair_role(
        guild, id1, id2,
        sep=COUPLE_SEPARATOR, colour=COUPLE_ROLE_COLOUR,
        noun="Couple", reason="Just married", headline="created for you both!",
    )


async def _add_pair_role(guild: discord.Guild | None, id1: int, id2: int, *, sep: str, colour: int, noun: str, reason: str, headline: str) -> str:
    if guild is None:
        return ""
    # The two lookups are independent Discord calls — together, not in a row.
    mem_a, mem_b = await asyncio.gather(
        _resolve_member(guild, id1), _resolve_member(guild, id2)
    )
    if mem_a is None or mem_b is None:
        return ""
    role = await _get_or_create_pair_role(guild, mem_a, mem_b, sep=sep, colour=colour, noun=noun)
    if role is None:
        return ""
    for m in (mem_a, mem_b):
        try:
            if role not in getattr(m, "roles", []):
                await m.add_roles(role, reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            continue
    return f"\n\n{role.mention} (`{role.name}`) {headline}"


async def _cleanup_couple_roles_on_divorce(guild: discord.Guild | None, id1: int, id2: int) -> str:
    if guild is None:
        return ""
    note = await _cleanup_pair_roles(
        guild, id1, id2,
        sep=COUPLE_SEPARATOR, colour=COUPLE_ROLE_COLOUR,
        reason="Divorced", delete_reason="Couple divorced", label="couple role",
    )
    # 2. Migration: strip legacy shared role if now single.
    try:
        legacy = discord.utils.get(guild.roles, name=MARRIED_ROLE_NAME)
        if legacy is not None:
            import aiosqlite as _sqlite

            from utils import db as _db

            for mid in (id1, id2):
                try:
                    async with _sqlite.connect(_db.DB_PATH) as conn:
                        async with conn.execute(
                            "SELECT 1 FROM marriages WHERE guild_id=? AND user1_id=? LIMIT 1",
                            (guild.id, mid),
                        ) as cur:
                            if await cur.fetchone():
                                continue
                except Exception:
                    continue
                m = await _resolve_member(guild, mid)
                if m is not None and legacy in getattr(m, "roles", []):
                    try:
                        await m.remove_roles(legacy, reason="Divorced")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
    except Exception:
        pass
    return note


async def _cleanup_pair_roles(guild: discord.Guild | None, id1: int, id2: int, *, sep: str, colour: int, reason: str, delete_reason: str, label: str) -> str:
    if guild is None:
        return ""
    removed_names: list[str] = []
    mem_a, mem_b = await asyncio.gather(
        _resolve_member(guild, id1), _resolve_member(guild, id2)
    )
    members = {id1: mem_a, id2: mem_b}
    for role in list(getattr(guild, "roles", [])):
        try:
            if sep not in role.name:
                continue
            if getattr(role, "colour", None) is not None and role.colour.value != colour:
                # Only auto-delete roles we created (matching colour).
                # Still remove the role from the pair even if colour differs,
                # but never delete foreign roles.
                foreign = True
            else:
                foreign = False
            try:
                member_ids = {m.id for m in role.members}
            except Exception:
                member_ids = set()
            if id1 not in member_ids and id2 not in member_ids:
                continue
            for mid in (id1, id2):
                m = members.get(mid)
                if m is not None and role in getattr(m, "roles", []):
                    try:
                        await m.remove_roles(role, reason=reason)
                    except (discord.Forbidden, discord.HTTPException):
                        pass
            removed_names.append(f"`{role.name}`")
            if foreign:
                continue
            try:
                fresh_members = [mm for mm in role.members if mm.id not in (id1, id2)]
            except Exception:
                fresh_members = []
            if not fresh_members:
                try:
                    await role.delete(reason=delete_reason)
                except (discord.Forbidden, discord.HTTPException):
                    pass
        except Exception:
            continue
    if not removed_names:
        return ""
    return f"\n\n{label} {', '.join(removed_names)} removed."


class MarriageView(discord.ui.View):
    def __init__(self, proposer: discord.Member, target: discord.Member, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.proposer = proposer
        self.target = target
        self.responded = False
        self.message: discord.Message | None = None

    async def on_timeout(self):
        if self.responded:
            return
        self.responded = True
        for item in self.children:
            item.disabled = True
        expired = discord.Embed(
            title="💍 Marriage Proposal — Expired",
            description=(
                f"{self.proposer.mention} proposed to {self.target.mention}, "
                "but they didn't respond in time.\n*Run `/marry` again to repropose.*"
            ),
            color=0x95A5A6,
            timestamp=discord.utils.utcnow()
        )
        try:
            if self.message is not None:
                await self.message.edit(embed=expired, view=self)
        except Exception:
            pass
        finally:
            self.stop()

    @discord.ui.button(label="💍 Accept Proposal", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message("❌ This proposal is not for you!", ephemeral=True)
        if self.responded:
            return await interaction.response.send_message("❌ This proposal has already been answered or expired.", ephemeral=True)
        self.responded = True
        for item in self.children:
            item.disabled = True

        now = discord.utils.utcnow().strftime("%B %d, %Y")
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO marriages (guild_id, user1_id, user2_id, married_at) VALUES (?, ?, ?, ?)",
                (interaction.guild.id, self.proposer.id, self.target.id, now)
            )
            await conn.execute(
                "INSERT OR REPLACE INTO marriages (guild_id, user1_id, user2_id, married_at) VALUES (?, ?, ?, ?)",
                (interaction.guild.id, self.target.id, self.proposer.id, now)
            )
            # Dating upgrade, decided in the same connection: marrying your
            # bf/gf absorbs the dating bond, so the pair keeps one bond.
            upgrading = False
            async with conn.execute(
                "SELECT user2_id FROM bonds WHERE guild_id=? AND user1_id=? AND kind='dating'",
                (interaction.guild.id, self.proposer.id),
            ) as cur:
                row = await cur.fetchone()
                upgrading = row is not None and row[0] == self.target.id
            if upgrading:
                await conn.execute(
                    "DELETE FROM bonds WHERE guild_id=? AND kind='dating' AND (user1_id=? OR user2_id=?)",
                    (interaction.guild.id, self.proposer.id, self.proposer.id),
                )
            await conn.commit()

        role_note = await _add_couple_role(interaction.guild, self.proposer.id, self.target.id)

        # Dating upgrade: marrying your bf/gf absorbs the dating bond and its
        # role, so the pair keeps exactly one bond and one role.
        if upgrading:
            dating = BOND_KINDS["dating"]
            upgrade_note = await _cleanup_pair_roles(
                interaction.guild, self.proposer.id, self.target.id,
                sep=dating["sep"], colour=dating["colour"],
                reason="Upgraded to marriage", delete_reason="Dating upgraded to marriage",
                label="dating role",
            )
            if upgrade_note:
                role_note += f"\n*Dating bond upgraded to marriage!*{upgrade_note}"

        embed = discord.Embed(
            title="💒 JUST MARRIED! 💍",
            description=(
                f"🎉 **{self.target.mention} said YES to {self.proposer.mention}!**\n\n"
                f"🔔 The church bells are ringing across the server!\n"
                f"May your love be eternal and your coin balance never hit zero! ✨\n\n"
                f"📅 **Wedding Date:** `{now}`{role_note}"
            ),
            color=0xEB459E,
            timestamp=discord.utils.utcnow()
        )
        # Verified or absent: the hand-typed tenor link that used to be here
        # answered 404, so the wedding card was drawn with a broken image.
        wedding_gif = await gifs.action_gif("wedding")
        if wedding_gif:
            embed.set_image(url=wedding_gif)
        embed.set_footer(text="DaCat Love & Marriage")
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

    @discord.ui.button(label="💔 Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message("❌ This proposal is not for you!", ephemeral=True)
        if self.responded:
            return await interaction.response.send_message("❌ This proposal has already been answered or expired.", ephemeral=True)
        self.responded = True
        for item in self.children:
            item.disabled = True

        embed = discord.Embed(
            title="💔 Proposal Rejected",
            description=f"Oof... {self.target.mention} turned down {self.proposer.mention}'s marriage proposal.\n*F in the chat for our fallen soldier.* 🪦",
            color=0xED4245,
            timestamp=discord.utils.utcnow()
        )
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()


# -------------------------------------------------------------
# RELATIONSHIP BONDS (Dating, Besties, Twins, Siblings & more)
# -------------------------------------------------------------
# Marriages are forever (well, `/divorce`), but not every bond is a wedding:
# boyfriend/girlfriend, best friends, twins and the rest get the same
# proposal ceremony, pair role and breakup flow, keyed by ``kind`` in the
# ``bonds`` table instead of the ``marriages`` one.
BOND_KINDS: dict[str, dict] = {
    "dating": {
        "emoji": "💘",
        "noun": "Dating",
        "verb": "dating",
        "colour": 0xFF6B81,
        "sep": " ❤ ",
        "gif": "kiss",
        "propose_title": "💘 Dating Proposal",
        "propose_text": "{p} asked {t} to be their boyfriend/girlfriend! Say yes and make it official! 💘",
        "accept_title": "💘 OFFICIALLY DATING!",
        "accept_text": "{t} said **YES** to {p}! The whole server ships it! 🚢💘",
    },
    "bestie": {
        "emoji": "🧸",
        "noun": "Bestie",
        "verb": "best friends",
        "colour": 0xFDCB6E,
        "sep": " 🤝 ",
        "gif": "hug",
        "propose_title": "🧸 Best-Friend Proposal",
        "propose_text": "{p} wants {t} as their one-and-only best friend! Through thick and thin! 🧸",
        "accept_title": "🧸 BEST FRIENDS FOREVER!",
        "accept_text": "{t} accepted! {p} & {t} are officially besties! Friendship bracelets for everyone! 🌈",
    },
    "twin": {
        "emoji": "👯",
        "noun": "Twin",
        "verb": "twins",
        "colour": 0xA29BFE,
        "sep": " 👯 ",
        "gif": "hug",
        "propose_title": "👯 Twin Proposal",
        "propose_text": "{p} claims {t} as their long-lost twin! Same wavelength, same chaos! 👯",
        "accept_title": "👯 TWINS CONFIRMED!",
        "accept_text": "{t} accepted! {p} & {t} are officially twins! Double trouble from now on! ✨",
    },
    "sibling": {
        "emoji": "🧬",
        "noun": "Sibling",
        "verb": "siblings",
        "colour": 0x81ECEC,
        "sep": " 🧬 ",
        "gif": "hug",
        "propose_title": "🧬 Sibling Proposal",
        "propose_text": "{p} wants {t} as their sibling — family you actually get to choose! 🧬",
        "accept_title": "🧬 OFFICIAL SIBLINGS!",
        "accept_text": "{t} accepted! {p} & {t} are officially siblings! Sunday dinners forever! 🍽️",
    },
    "crush": {
        "emoji": "💖",
        "noun": "Crush",
        "verb": "crushing",
        "colour": 0xFD79A8,
        "sep": " 💖 ",
        "gif": "blush",
        "propose_title": "💖 Crush Confession",
        "propose_text": "{p} just confessed their crush on {t}! The butterflies are real! 🦋",
        "accept_title": "💖 CRUSH CONFIRMED!",
        "accept_text": "{t} admitted they feel it too! {p} & {t} are officially crushing! 😳💖",
    },
    "rival": {
        "emoji": "⚔️",
        "noun": "Rival",
        "verb": "rivals",
        "colour": 0xE17055,
        "sep": " ⚔️ ",
        "gif": "stare",
        "propose_title": "⚔️ Rival Challenge",
        "propose_text": "{p} challenged {t} to be their rival! May the best one win! ⚔️",
        "accept_title": "⚔️ RIVALRY ACCEPTED!",
        "accept_text": "{t} accepted the challenge! {p} vs {t} — this means war (the friendly kind)! 🔥",
    },
}


async def _ensure_bonds_table() -> None:
    # A live database can predate this table: the file the process holds may
    # come from a backup written before the migration existed, and setup runs
    # only at boot (see utils.meetings, which heals its tables the same way).
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute('''CREATE TABLE IF NOT EXISTS bonds (
            guild_id INTEGER,
            user1_id INTEGER,
            user2_id INTEGER,
            kind TEXT,
            since_at TEXT,
            PRIMARY KEY (guild_id, user1_id, kind)
        )''')
        await conn.commit()


async def _bond_partner(guild_id: int, user_id: int, kind: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT user2_id FROM bonds WHERE guild_id=? AND user1_id=? AND kind=?",
            (guild_id, user_id, kind),
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def _user_bonds(guild_id: int, user_id: int) -> list[tuple[str, int]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT kind, user2_id FROM bonds WHERE guild_id=? AND user1_id=?",
            (guild_id, user_id),
        ) as cur:
            return [(r[0], r[1]) async for r in cur]


async def _delete_bond_rows(guild_id: int, user_id: int, kind: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "DELETE FROM bonds WHERE guild_id=? AND kind=? AND (user1_id=? OR user2_id=?)",
            (guild_id, kind, user_id, user_id),
        )
        await conn.commit()


async def _proposal_state(guild_id: int, me_id: int, them_id: int) -> dict:
    """Every relationship row a proposal checks, in a single connection.

    A proposal used to open one SQLite connection per question (whose bond,
    whose marriage, what the pair shares) — five roundtrips of open, read,
    close before the card even renders. One connection, two statements.
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute('''CREATE TABLE IF NOT EXISTS bonds (
            guild_id INTEGER,
            user1_id INTEGER,
            user2_id INTEGER,
            kind TEXT,
            since_at TEXT,
            PRIMARY KEY (guild_id, user1_id, kind)
        )''')
        async with conn.execute(
            "SELECT user1_id, user2_id FROM marriages WHERE guild_id=? AND user1_id IN (?, ?)",
            (guild_id, me_id, them_id),
        ) as cur:
            married = {r[0]: r[1] async for r in cur}
        async with conn.execute(
            "SELECT user1_id, user2_id, kind FROM bonds WHERE guild_id=? AND user1_id IN (?, ?)",
            (guild_id, me_id, them_id),
        ) as cur:
            bonds = [(r[0], r[1], r[2]) async for r in cur]
    my_bonds = {kind: partner for (user, partner, kind) in bonds if user == me_id}
    their_bonds = {kind: partner for (user, partner, kind) in bonds if user == them_id}
    return {
        "married_me": married.get(me_id),
        "married_them": married.get(them_id),
        "my_bonds": my_bonds,
        "their_bonds": their_bonds,
        "pair_kinds": {
            kind
            for (user, partner, kind) in bonds
            if (user == me_id and partner == them_id)
            or (user == them_id and partner == me_id)
        },
    }


class BondView(discord.ui.View):
    """Accept / Decline for a relationship proposal — the MarriageView pattern."""

    def __init__(self, proposer: discord.Member, target: discord.Member, kind: str, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.proposer = proposer
        self.target = target
        self.kind = kind
        self.responded = False
        self.message: discord.Message | None = None
        emoji = BOND_KINDS[kind]["emoji"]
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.style == discord.ButtonStyle.success:
                item.label = f"{emoji} Accept"

    @property
    def spec(self) -> dict:
        return BOND_KINDS[self.kind]

    async def on_timeout(self):
        if self.responded:
            return
        self.responded = True
        for item in self.children:
            item.disabled = True
        spec = self.spec
        expired = discord.Embed(
            title=f"{spec['emoji']} {spec['noun']} Proposal — Expired",
            description=(
                f"{self.proposer.mention} proposed {spec['verb']} with {self.target.mention}, "
                "but they didn't respond in time.\n*Run `/bond propose` again to retry.*"
            ),
            color=0x95A5A6,
            timestamp=discord.utils.utcnow()
        )
        try:
            if self.message is not None:
                await self.message.edit(embed=expired, view=self)
        except Exception:
            pass
        finally:
            self.stop()

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        spec = self.spec
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message("❌ This proposal is not for you!", ephemeral=True)
        if self.responded:
            return await interaction.response.send_message("❌ This proposal has already been answered or expired.", ephemeral=True)
        self.responded = True
        for item in self.children:
            item.disabled = True

        await _ensure_bonds_table()
        now = discord.utils.utcnow().strftime("%B %d, %Y")
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO bonds (guild_id, user1_id, user2_id, kind, since_at) VALUES (?, ?, ?, ?, ?)",
                (interaction.guild.id, self.proposer.id, self.target.id, self.kind, now)
            )
            await conn.execute(
                "INSERT OR REPLACE INTO bonds (guild_id, user1_id, user2_id, kind, since_at) VALUES (?, ?, ?, ?, ?)",
                (interaction.guild.id, self.target.id, self.proposer.id, self.kind, now)
            )
            await conn.commit()

        role_note = await _add_pair_role(
            interaction.guild, self.proposer.id, self.target.id,
            sep=spec["sep"], colour=spec["colour"], noun=spec["noun"],
            reason=f"Bonded ({self.kind})", headline=f"are now {spec['verb']}!",
        )

        embed = discord.Embed(
            title=spec["accept_title"],
            description=(
                spec["accept_text"].format(p=self.proposer.mention, t=self.target.mention)
                + f"\n\n📅 **Since:** `{now}`{role_note}"
            ),
            color=spec["colour"],
            timestamp=discord.utils.utcnow()
        )
        bond_gif = await gifs.action_gif(spec["gif"])
        if bond_gif:
            embed.set_image(url=bond_gif)
        embed.set_footer(text=f"DaCat Bonds • {spec['noun']}")
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

    @discord.ui.button(label="💔 Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        spec = self.spec
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message("❌ This proposal is not for you!", ephemeral=True)
        if self.responded:
            return await interaction.response.send_message("❌ This proposal has already been answered or expired.", ephemeral=True)
        self.responded = True
        for item in self.children:
            item.disabled = True

        embed = discord.Embed(
            title=f"💔 {spec['noun']} Proposal Rejected",
            description=f"Oof... {self.target.mention} turned down {self.proposer.mention}. *F in the chat.* 🪦",
            color=0xED4245,
            timestamp=discord.utils.utcnow()
        )
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()


# -------------------------------------------------------------
# SOCIAL & SAFARI COG IMPLEMENTATION
# -------------------------------------------------------------
class Social(commands.Cog):
    """Pet safari hunting, zoo collection, rocket crash game, social roleplay actions, and marriage system!"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            cmd_name = interaction.command.name if interaction.command else None
            if cmd_name == "crash":
                for mod in ["economy", "games"]:
                    if not self.bot.is_module_enabled(interaction.guild_id, mod):
                        await interaction.response.send_message(
                            f"⚠️ The **{mod.title()}** system is currently **turned off** on this server.\n"
                            "An administrator can re-enable it in the [Web Dashboard](https://dacatdashboard.pages.dev/dashboard).",
                            ephemeral=True
                        )
                        return False
            elif cmd_name == "sellanimals":
                for mod in ["social", "economy"]:
                    if not self.bot.is_module_enabled(interaction.guild_id, mod):
                        await interaction.response.send_message(
                            f"⚠️ The **{mod.title()}** system is currently **turned off** on this server.\n"
                            "An administrator can re-enable it in the [Web Dashboard](https://dacatdashboard.pages.dev/dashboard).",
                            ephemeral=True
                        )
                        return False
            else:
                if not self.bot.is_module_enabled(interaction.guild_id, "social"):
                    await interaction.response.send_message(
                        "⚠️ The **Social Roleplay** system is currently **turned off** on this server.\n"
                        "An administrator can re-enable it in the [Web Dashboard](https://dacatdashboard.pages.dev/dashboard).",
                        ephemeral=True
                    )
                    return False
        return True

    # ---------------------------------------------------------
    # 1. ANIMAL HUNTING & ZOO
    # ---------------------------------------------------------
    @app_commands.command(name="hunt", description="Go hunting in the wild and catch rare animals for your zoo!")
    @app_commands.checks.cooldown(1, HUNT_COOLDOWN_SECONDS, key=lambda i: (i.guild_id, i.user.id))
    async def hunt(self, interaction: discord.Interaction):
        await ensure_eco(interaction.user.id, interaction.guild.id)

        # Catch 1 to 3 animals per hunt
        num_catches = random.choices([1, 2, 3], weights=[0.40, 0.45, 0.15])[0]
        catches = []

        tier_weights = {
            "common": 0.52,
            "uncommon": 0.26,
            "rare": 0.14,
            "epic": 0.055,
            "mythic": 0.02,
            "legendary": 0.005,
        }
        tiers = list(tier_weights.keys())
        weights = list(tier_weights.values())

        async with aiosqlite.connect(DB_PATH) as conn:
            for _ in range(num_catches):
                chosen_tier = random.choices(tiers, weights=weights)[0]
                emoji, name, val = random.choice(ANIMALS[chosen_tier])
                catches.append((chosen_tier, emoji, name, val))

                await conn.execute("""
                    INSERT INTO user_animals (guild_id, user_id, animal_name, emoji, tier, count)
                    VALUES (?, ?, ?, ?, ?, 1)
                    ON CONFLICT(guild_id, user_id, animal_name)
                    DO UPDATE SET count = count + 1
                """, (interaction.guild.id, interaction.user.id, name, emoji, chosen_tier))
            await conn.commit()

        # Bonus pocket coins found on hunt
        bonus = random.randint(40, 220)
        await update_wallet(interaction.user.id, interaction.guild.id, bonus)

        # Highest tier caught determines embed color
        highest_tier = "common"
        for t, _, _, _ in catches:
            if list(ANIMALS.keys()).index(t) > list(ANIMALS.keys()).index(highest_tier):
                highest_tier = t

        embed = discord.Embed(
            title="🌲 Hunting Expedition Results",
            description="You ventured deep into the wilderness and brought back:\n",
            color=TIER_COLORS[highest_tier],
            timestamp=discord.utils.utcnow()
        )

        for tier, emoji, name, val in catches:
            embed.description += f"• {emoji} **{name}** — {TIER_NAMES[tier]} *(Value: {val:,} 🪙)*\n"

        embed.description += f"\n🪙 You also scavenged **+{bonus:,}** coins from the wild!"
        embed.set_footer(text="Tip: Run /zoo to inspect your collection or /sellanimals to cash out!")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="zoo", description="View your or another member's caught animals collection.")
    @app_commands.describe(member="Member's zoo to view")
    async def zoo(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user

        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT animal_name, emoji, tier, count FROM user_animals WHERE guild_id=? AND user_id=? ORDER BY count DESC",
                (interaction.guild.id, member.id)
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            embed = discord.Embed(
                title=f"{member.display_name}'s Zoo is Empty!",
                description="No animals caught yet! Run `/hunt` to begin your wilderness safari.",
                color=0x95A5A6
            )
            return await send_card(interaction, embed=embed)

        # Organize by tier
        tier_buckets = {"legendary": [], "mythic": [], "epic": [], "rare": [], "uncommon": [], "common": []}
        total_animals = 0
        total_value = 0

        # Lookup price map
        price_map = {}
        for list_a in ANIMALS.values():
            for _emoji, name, price in list_a:
                price_map[name] = price

        for name, emoji, tier, count in rows:
            total_animals += count
            total_value += count * price_map.get(name, 50)
            tier_buckets.get(tier, []).append(f"{emoji} {name} `x{count}`")

        embed = discord.Embed(
            title=f"🦁 {member.display_name}'s Wildlife Zoo",
            description=f"**Total Animals Caught:** `{total_animals:,}` | **Est. Value:** `{total_value:,}` 🪙\n",
            color=0x2ECC71,
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        for tier in ["legendary", "mythic", "epic", "rare", "uncommon", "common"]:
            items = tier_buckets[tier]
            if items:
                embed.add_field(
                    name=f"{TIER_NAMES[tier]} ({len(items)} species)",
                    value=" • ".join(items[:15]) + ("..." if len(items) > 15 else ""),
                    inline=False
                )

        embed.set_footer(text="DaCat Zoo • Run /sellanimals to exchange animals for coins")
        await send_card(interaction, embed=embed)

    @app_commands.command(name="sellanimals", description="Sell caught zoo animals for coins.")
    @app_commands.describe(tier="Which tier to sell")
    @app_commands.choices(tier=[
        app_commands.Choice(name="All Common Animals", value="common"),
        app_commands.Choice(name="All Uncommon Animals", value="uncommon"),
        app_commands.Choice(name="All Rare Animals", value="rare"),
        app_commands.Choice(name="Common & Uncommon (Keep Rare+)", value="common_uncommon"),
        app_commands.Choice(name="Sell Everything (Full Liquidation)", value="all"),
    ])
    async def sellanimals(self, interaction: discord.Interaction, tier: str):
        price_map = {}
        for list_a in ANIMALS.values():
            for _emoji, name, price in list_a:
                price_map[name] = price

        if tier == "common":
            query = "SELECT animal_name, count FROM user_animals WHERE guild_id=? AND user_id=? AND tier='common'"
            delete_query = "DELETE FROM user_animals WHERE guild_id=? AND user_id=? AND tier='common'"
        elif tier == "uncommon":
            query = "SELECT animal_name, count FROM user_animals WHERE guild_id=? AND user_id=? AND tier='uncommon'"
            delete_query = "DELETE FROM user_animals WHERE guild_id=? AND user_id=? AND tier='uncommon'"
        elif tier == "rare":
            query = "SELECT animal_name, count FROM user_animals WHERE guild_id=? AND user_id=? AND tier='rare'"
            delete_query = "DELETE FROM user_animals WHERE guild_id=? AND user_id=? AND tier='rare'"
        elif tier == "common_uncommon":
            query = "SELECT animal_name, count FROM user_animals WHERE guild_id=? AND user_id=? AND tier IN ('common', 'uncommon')"
            delete_query = "DELETE FROM user_animals WHERE guild_id=? AND user_id=? AND tier IN ('common', 'uncommon')"
        else:
            query = "SELECT animal_name, count FROM user_animals WHERE guild_id=? AND user_id=?"
            delete_query = "DELETE FROM user_animals WHERE guild_id=? AND user_id=?"

        await ensure_eco(interaction.user.id, interaction.guild.id)

        def _sell(cur):
            """Read, delete and pay inside one transaction.

            The rows were read on one connection and the coins credited on
            another, so two simultaneous sales of the same animals both computed
            a price from the same rows — the first deleted them, the second
            deleted nothing and was still paid.
            """
            cur.execute(query, (interaction.guild.id, interaction.user.id))
            rows = cur.fetchall()
            if not rows:
                return None
            total_coins = 0
            total_count = 0
            for name, count in rows:
                total_coins += price_map.get(name, 50) * count
                total_count += count
            cur.execute(delete_query, (interaction.guild.id, interaction.user.id))
            if cur.rowcount != len(rows):
                raise RuntimeError(
                    f"animal rows changed during sale ({cur.rowcount} of {len(rows)} removed)"
                )
            cur.execute(
                "UPDATE economy SET wallet = wallet + ? WHERE user_id=? AND guild_id=?",
                (total_coins, interaction.user.id, interaction.guild.id),
            )
            return total_coins, total_count

        sold = await asyncio.to_thread(db.run_atomic, _sell)
        if not sold:
            return await interaction.response.send_message(
                "❌ You don't have any animals in that category to sell!", ephemeral=True
            )
        total_coins, total_count = sold

        embed = discord.Embed(
            title="💰 Animals Sold!",
            description=f"✅ You sold **{total_count:,}** animals and received **+{total_coins:,}** 🪙 in your wallet!",
            color=0x57F287,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="DaCat Market")
        await send_card(interaction, embed=embed)

    # ---------------------------------------------------------
    # 2. HIGH-STAKES CRASH GAME
    # ---------------------------------------------------------
    @app_commands.command(name="crash", description="High-stakes rocket crash game! Cash out before the rocket explodes!")
    @app_commands.describe(bet="Amount of coins to bet")
    async def crash(self, interaction: discord.Interaction, bet: app_commands.Range[int, 1, None]):
        await ensure_eco(interaction.user.id, interaction.guild.id)
        eco = await get_eco(interaction.user.id, interaction.guild.id)
        if eco["wallet"] < bet:
            return await interaction.response.send_message(f"❌ You only have **{eco['wallet']:,}** 🪙 in your wallet.", ephemeral=True)

        # Hold the stake atomically. A plain update_wallet(-bet) clamps at zero,
        # so two /crash games fired at the same instant could each start without
        # really paying for themselves.
        if not await take_stake(interaction.user.id, interaction.guild.id, bet):
            return await interaction.response.send_message(
                "❌ You don't have enough coins to cover that bet.", ephemeral=True
            )

        # Realistic crash curve
        if random.random() < 0.08:
            crash_point = 1.00  # 8% instant bust
        else:
            crash_point = round(1.0 + random.expovariate(0.65), 2)
            crash_point = max(1.02, min(crash_point, 50.00))

        view = CrashView(interaction.user.id, interaction.guild.id, bet, crash_point)

        embed = discord.Embed(
            title="🚀 Rocket Crash Game",
            description=(
                f"**The rocket is preparing for launch!**\n\n"
                f"• **Bet Placed:** `{bet:,}` 🪙\n"
                f"• **Current Multiplier:** `🚀 1.00x`\n"
                f"• **Potential Win:** `{bet:,}` 🪙\n\n"
                f"*Click **Cash Out** before the rocket explodes!*"
            ),
            color=0x5865F2,
            timestamp=discord.utils.utcnow()
        )
        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()

        # Step progression
        current = 1.00
        step = 0.15
        while current < crash_point:
            await asyncio.sleep(1.2)
            if view.cashed_out:
                return  # Handled by view button callback

            current = round(current + step, 2)
            step += 0.05
            if current >= crash_point:
                break

            view.multiplier = current
            view.children[0].label = f"🛑 Cash Out (+{int(bet * current):,} 🪙)"
            embed.description = (
                f"**The rocket is soaring higher into space!**\n\n"
                f"• **Bet Placed:** `{bet:,}` 🪙\n"
                f"• **Current Multiplier:** `🚀 {current:.2f}x`\n"
                f"• **Current Value:** `+{int(bet * current):,}` 🪙\n\n"
                f"*Click **Cash Out** before the rocket explodes!*"
            )
            try:
                await msg.edit(embed=embed, view=view)
            except Exception as e:
                # The card can no longer be updated (deleted, or the API refused).
                # The round cannot be played, so end it and refund the stake. The
                # old code returned here with the buttons still live, which let a
                # player cash out at a multiplier the rocket may never have had.
                print(f"[CRASH] Could not update the game card: {e}")
                view.ended = True
                view.clear_items()
                await update_wallet(interaction.user.id, interaction.guild.id, bet)
                void_embed = discord.Embed(
                    title="🚀 Launch Aborted",
                    description=(
                        "The game card could not be updated, so the round was cancelled.\n"
                        f"Your **{bet:,}** 🪙 bet has been **refunded**."
                    ),
                    color=0xFEE75C,
                    timestamp=discord.utils.utcnow(),
                )
                try:
                    await msg.edit(embed=void_embed, view=view)
                except Exception:
                    pass
                return

        # If reached here without cashing out, rocket exploded
        if not view.cashed_out:
            view.ended = True
            view.clear_items()
            crash_embed = discord.Embed(
                title="💥 ROCKET EXPLODED!",
                description=(
                    f"💀 **BOOM! The rocket crashed at `{crash_point:.2f}x`!**\n\n"
                    f"You did not cash out in time and lost your **{bet:,}** 🪙 bet.\n"
                    f"*Better luck on the next launch!*"
                ),
                color=0xED4245,
                timestamp=discord.utils.utcnow()
            )
            crash_embed.set_footer(text=f"Played by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            try:
                await msg.edit(embed=crash_embed, view=view)
            except Exception:
                pass

    # ---------------------------------------------------------
    # 3. SOCIAL ROLEPLAY ACTIONS
    # ---------------------------------------------------------
    async def _handle_action(self, interaction: discord.Interaction, action: str, member: discord.Member):
        if member.id == interaction.user.id:
            return await interaction.response.send_message(f"❌ You can't {action} yourself!", ephemeral=True)

        # The picture is fetched and verified first, so the reply is deferred up
        # front rather than answering twice on a slow provider.
        await interaction.response.defer()

        data = ACTIONS_DATA[action]
        line = random.choice(data["lines"]).format(author=interaction.user.mention, target=member.mention)

        embed = discord.Embed(
            title=data["title"],
            description=line,
            color=data["color"],
            timestamp=discord.utils.utcnow()
        )

        # Verified, or nothing: an unconfirmed link used to be drawn as an empty
        # placeholder, which looks like the bot is broken (see utils/gifs).
        gif = await gifs.action_gif(action)
        if gif:
            embed.set_image(url=gif)

        embed.set_footer(text=f"{interaction.user.display_name} → {member.display_name}")

        # Cards suppress mentions by default, and this one *is* about somebody who
        # is supposed to be told. The target is the only person notified: pinging
        # the author back at themselves is noise nobody asked for.
        await send_card(interaction, embed=embed, allowed_mentions=pings(member))

    @app_commands.command(name="action", description="Perform an anime/social roleplay action with another member!")
    @app_commands.describe(action="The roleplay action to perform", member="The member to interact with")
    @app_commands.choices(action=[
        app_commands.Choice(name="🔨 Bonk", value="bonk"),
        app_commands.Choice(name="👋 Slap", value="slap"),
        app_commands.Choice(name="🤗 Hug", value="hug"),
        app_commands.Choice(name="💋 Kiss", value="kiss"),
        app_commands.Choice(name="✨ Headpat", value="pat"),
        app_commands.Choice(name="👊 Punch", value="punch"),
        app_commands.Choice(name="🦷 Bite", value="bite"),
        app_commands.Choice(name="🧸 Cuddle", value="cuddle"),
        app_commands.Choice(name="👉 Poke", value="poke"),
        app_commands.Choice(name="🍰 Feed", value="feed"),
        app_commands.Choice(name="😊 Blush", value="blush"),
        app_commands.Choice(name="😄 Smile", value="smile"),
        app_commands.Choice(name="👋 Wave", value="wave"),
        app_commands.Choice(name="😉 Wink", value="wink"),
        app_commands.Choice(name="💃 Dance", value="dance"),
        app_commands.Choice(name="😂 Laugh", value="laugh"),
        app_commands.Choice(name="😭 Cry", value="cry"),
        app_commands.Choice(name="👅 Lick", value="lick"),
        app_commands.Choice(name="🪶 Tickle", value="tickle"),
    ])
    async def action(self, interaction: discord.Interaction, action: app_commands.Choice[str], member: discord.Member):
        await self._handle_action(interaction, action.value, member)

    # ---------------------------------------------------------
    # 4. MARRIAGE & LOVE SYSTEM
    # ---------------------------------------------------------
    @app_commands.command(name="marry", description="Propose marriage to another member!")
    @app_commands.describe(member="The lucky member to propose to")
    async def marry(self, interaction: discord.Interaction, member: discord.Member):
        if member.id == interaction.user.id:
            return await interaction.response.send_message("❌ You can't marry yourself!", ephemeral=True)
        if member.bot:
            return await interaction.response.send_message("❌ Bots cannot sign marriage certificates!", ephemeral=True)

        # One connection for every check, flying alongside the GIF fetch.
        state, ring_gif = await asyncio.gather(
            _proposal_state(interaction.guild.id, interaction.user.id, member.id),
            gifs.action_gif("propose"),
        )
        if state["married_me"] is not None:
            return await interaction.response.send_message("❌ You are already married! Run `/divorce` first.", ephemeral=True)
        if state["married_them"] is not None:
            return await interaction.response.send_message(f"❌ {member.mention} is already married to someone else!", ephemeral=True)

        my_date = state["my_bonds"].get("dating")
        if my_date is not None and my_date != member.id:
            return await interaction.response.send_message(
                "❌ You're already dating someone! Run `/bond break` first — or propose to them instead! 💘", ephemeral=True)
        their_date = state["their_bonds"].get("dating")
        if their_date is not None and their_date != interaction.user.id:
            return await interaction.response.send_message(
                f"❌ {member.mention} is already dating someone else!", ephemeral=True)

        embed = discord.Embed(
            title="💍 Marriage Proposal",
            description=f"💖 {interaction.user.mention} has fallen to one knee and proposed to {member.mention}!\n\n*{member.mention}, will you take their hand in holy matrimony?*",
            color=0xEB459E,
            timestamp=discord.utils.utcnow()
        )
        # Same verified-or-absent rule as the actions: this was another dead
        # tenor link. Full width rather than the thumbnail it used to be, because
        # this is the card people screenshot and a 60px corner was the part of it
        # nobody could see.
        if ring_gif:
            embed.set_image(url=ring_gif)
        view = MarriageView(interaction.user, member)
        # A legacy send with the mention in ``content``, which is what actually
        # notifies — the proposal is useless if the person proposed to misses it.
        await interaction.response.send_message(content=member.mention, embed=embed, view=view)
        try:
            view.message = await interaction.original_response()
        except Exception:
            pass

    @app_commands.command(name="divorce", description="Divorce your current spouse.")
    async def divorce(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute("SELECT user2_id FROM marriages WHERE guild_id=? AND user1_id=?", (interaction.guild.id, interaction.user.id)) as cur:
                row = await cur.fetchone()
            if not row:
                return await interaction.response.send_message("❌ You are not married to anyone!", ephemeral=True)

            spouse_id = row[0]
            await conn.execute("DELETE FROM marriages WHERE guild_id=? AND (user1_id=? OR user2_id=?)", (interaction.guild.id, interaction.user.id, interaction.user.id))
            await conn.commit()

        # Plain name, not a mention: a mention inside a V2 card notifies, and an
        # uncached spouse would otherwise render as a raw "<@id>".
        s_name = await display_name(interaction.guild, spouse_id, client=interaction.client)

        role_note = await _cleanup_couple_roles_on_divorce(interaction.guild, interaction.user.id, spouse_id)

        embed = discord.Embed(
            title="📄 Divorce Finalized",
            description=f"💔 {interaction.user.mention} and {s_name} are officially divorced.\n*All good things come to an end...*{role_note}",
            color=0xED4245,
            timestamp=discord.utils.utcnow()
        )
        await send_card(interaction, embed=embed)

    # ---------------------------------------------------------
    # 4b. RELATIONSHIP BONDS (bf/gf, besties, twins & more)
    # ---------------------------------------------------------
    bond = app_commands.Group(
        name="bond",
        description="Dating, best friends, twins, siblings, crushes & rivals!",
    )

    @bond.command(name="propose", description="Propose a relationship bond to another member!")
    @app_commands.describe(member="The lucky member", kind="What kind of bond")
    @app_commands.choices(kind=[
        app_commands.Choice(name="💘 Dating (bf/gf)", value="dating"),
        app_commands.Choice(name="🧸 Bestie", value="bestie"),
        app_commands.Choice(name="👯 Twin", value="twin"),
        app_commands.Choice(name="🧬 Sibling", value="sibling"),
        app_commands.Choice(name="💖 Crush", value="crush"),
        app_commands.Choice(name="⚔️ Rival", value="rival"),
    ])
    async def bond_propose(self, interaction: discord.Interaction, member: discord.Member, kind: app_commands.Choice[str]):
        kind = kind.value
        spec = BOND_KINDS[kind]
        me_id, them_id = interaction.user.id, member.id
        if them_id == me_id:
            return await interaction.response.send_message(f"❌ You can't be your own {spec['noun'].lower()}!", ephemeral=True)
        if member.bot:
            return await interaction.response.send_message("❌ Bots cannot sign bond certificates!", ephemeral=True)

        # The database read (which heals the bonds table too) and the GIF
        # fetch are independent — one hits the disk, the other the network —
        # so they fly together, not in a row.
        state, bond_gif = await asyncio.gather(
            _proposal_state(interaction.guild.id, me_id, them_id),
            gifs.action_gif(spec["gif"]),
        )
        if kind in state["my_bonds"]:            return await interaction.response.send_message(
                f"❌ You already have a {spec['noun']}! Run `/bond break` first.", ephemeral=True)
        if kind in state["their_bonds"]:
            return await interaction.response.send_message(
                f"❌ {member.mention} already has a {spec['noun']} with someone else!", ephemeral=True)

        pair_kinds = state["pair_kinds"]
        if kind == "dating":
            if state["married_me"] is not None:
                return await interaction.response.send_message(
                    "❌ You're married! Run `/divorce` first — no cheating on my watch! 💍", ephemeral=True)
            if state["married_them"] is not None:
                return await interaction.response.send_message(
                    f"❌ {member.mention} is married to someone else!", ephemeral=True)
            if pair_kinds & {"twin", "sibling"}:
                return await interaction.response.send_message(
                    "❌ Family is family — you can't date your twin or sibling! 🚫", ephemeral=True)
        elif kind in ("twin", "sibling"):
            other = "sibling" if kind == "twin" else "twin"
            if other in pair_kinds:
                return await interaction.response.send_message(
                    f"❌ You're already {other}s — pick one family label!", ephemeral=True)
            if state["married_me"] == them_id:
                return await interaction.response.send_message(
                    "❌ You're married to them — that's a stronger bond already! 💍", ephemeral=True)
            if state["my_bonds"].get("dating") == them_id:
                return await interaction.response.send_message(
                    "❌ You're dating them — family-zoning your partner is diabolical! 💘", ephemeral=True)

        embed = discord.Embed(
            title=spec["propose_title"],
            description=f"{spec['emoji']} {interaction.user.mention} " + spec["propose_text"].format(p=interaction.user.mention, t=member.mention),
            color=spec["colour"],
            timestamp=discord.utils.utcnow()
        )
        if bond_gif:
            embed.set_image(url=bond_gif)
        view = BondView(interaction.user, member, kind)
        await interaction.response.send_message(content=member.mention, embed=embed, view=view)
        try:
            view.message = await interaction.original_response()
        except Exception:
            pass

    @bond.command(name="break", description="End one (or all) of your relationship bonds.")
    @app_commands.describe(kind="Which bond to end — leave empty if you only have one")
    @app_commands.choices(kind=[
        app_commands.Choice(name="💘 Dating (bf/gf)", value="dating"),
        app_commands.Choice(name="🧸 Bestie", value="bestie"),
        app_commands.Choice(name="👯 Twin", value="twin"),
        app_commands.Choice(name="🧬 Sibling", value="sibling"),
        app_commands.Choice(name="💖 Crush", value="crush"),
        app_commands.Choice(name="⚔️ Rival", value="rival"),
    ])
    async def break_bond(self, interaction: discord.Interaction, kind: app_commands.Choice[str] | None = None):
        await _ensure_bonds_table()
        mine = await _user_bonds(interaction.guild.id, interaction.user.id)
        if not mine:
            return await interaction.response.send_message("❌ You have no bonds to break! Forever alone... for now. 🥲", ephemeral=True)
        if kind is None:
            if len(mine) > 1:
                kinds = ", ".join(f"`{k}`" for k, _ in sorted(mine))
                return await interaction.response.send_message(
                    f"❌ You have several bonds ({kinds}) — run `/bond break` again naming which one!", ephemeral=True)
            kind_value = mine[0][0]
        else:
            kind_value = kind.value
        partner_id = await _bond_partner(interaction.guild.id, interaction.user.id, kind_value)
        if partner_id is None:
            return await interaction.response.send_message(f"❌ You have no `{kind_value}` bond!", ephemeral=True)
        spec = BOND_KINDS[kind_value]

        await _delete_bond_rows(interaction.guild.id, interaction.user.id, kind_value)
        role_note = await _cleanup_pair_roles(
            interaction.guild, interaction.user.id, partner_id,
            sep=spec["sep"], colour=spec["colour"],
            reason=f"{spec['noun']} bond ended", delete_reason=f"{spec['noun']} bond ended",
            label=f"{spec['noun'].lower()} role",
        )
        s_name = await display_name(interaction.guild, partner_id, client=interaction.client)
        embed = discord.Embed(
            title=f"{spec['emoji']} {spec['noun']} Bond Ended",
            description=f"💔 {interaction.user.mention} and {s_name} are no longer {spec['verb']}.\n*All good things come to an end...*{role_note}",
            color=0xED4245,
            timestamp=discord.utils.utcnow()
        )
        await send_card(interaction, embed=embed)

    @bond.command(name="list", description="Show someone's relationship bonds.")
    @app_commands.describe(member="Whose bonds to look up — defaults to you")
    async def bond_list(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        await _ensure_bonds_table()
        rows = await _user_bonds(interaction.guild.id, member.id)
        if not rows:
            whose = "You have" if member.id == interaction.user.id else f"{member.display_name} has"
            return await send_card(interaction, embed=discord.Embed(
                title="💞 Relationship Bonds",
                description=f"{whose} no bonds yet! Run `/bond propose` to start one! 🥲",
                color=0x95A5A6,
                timestamp=discord.utils.utcnow()
            ))
        lines = []
        for kind_value, partner_id in sorted(rows):
            spec = BOND_KINDS.get(kind_value)
            if spec is None:
                continue
            partner = await _resolve_member(interaction.guild, partner_id)
            pname = partner.display_name if partner else f"<@{partner_id}>"
            async with aiosqlite.connect(DB_PATH) as conn:
                async with conn.execute(
                    "SELECT since_at FROM bonds WHERE guild_id=? AND user1_id=? AND kind=?",
                    (interaction.guild.id, member.id, kind_value),
                ) as cur:
                    srow = await cur.fetchone()
            since = f" — since `{srow[0]}`" if srow and srow[0] else ""
            lines.append(f"{spec['emoji']} **{spec['noun']}** — {pname}{since}")
        await send_card(interaction, embed=discord.Embed(
            title=f"💞 {member.display_name}'s Bonds",
            description="\n".join(lines),
            color=0xEB459E,
            timestamp=discord.utils.utcnow()
        ))

    @app_commands.command(name="ship", description="Calculate the romantic compatibility between two members!")
    @app_commands.describe(user1="First member", user2="Second member")
    async def ship(self, interaction: discord.Interaction, user1: discord.Member, user2: discord.Member):
        # Deterministic match based on user IDs
        seed = (user1.id * 31 + user2.id * 17) % 101
        percent = seed

        filled = int(percent / 10)
        empty = 10 - filled
        bar = "❤️" * filled + "🖤" * empty

        # Blend names for ship name
        len1 = len(user1.display_name) // 2
        len2 = len(user2.display_name) // 2
        ship_name = user1.display_name[:max(2, len1)] + user2.display_name[max(2, len2):]

        if percent >= 90:
            verdict = "💍 **True Soulmates!** Start planning the wedding right now! 💒"
            color = 0xEB459E
        elif percent >= 75:
            verdict = "💖 **Match Made in Heaven!** The chemistry is off the charts! ✨"
            color = 0x57F287
        elif percent >= 50:
            verdict = "✨ **Cute Couple!** There is definitely a spark here! 🥰"
            color = 0xFEE75C
        elif percent >= 25:
            verdict = "🤔 **Friendzone Territory!** It could work, but requires heavy lifting! 🧗"
            color = 0xE67E22
        else:
            verdict = "💀 **Toxic Biohazard!** Keep at least 500 meters away from each other! ☣️"
            color = 0xED4245

        embed = discord.Embed(
            title="💘 Love Compatibility Scanner",
            description=(
                f"**Ship:** `{user1.display_name}` × `{user2.display_name}`\n"
                f"**Ship Name:** `{ship_name}`\n\n"
                f"**Compatibility:** `{percent}%`\n"
                f"`{bar}`\n\n"
                f"> {verdict}"
            ),
            color=color,
            timestamp=discord.utils.utcnow()
        )
        await send_card(interaction, embed=embed)

    # ---------------------------------------------------------
    # 5. AESTHETIC UWU TEXT TRANSLATOR
    # ---------------------------------------------------------
    @app_commands.command(name="uwu", description="Translate any sentence into cute aesthetic anime speak!")
    @app_commands.describe(text="The message to translate")
    async def uwu(self, interaction: discord.Interaction, text: str):
        # Basic substitution
        uwu_text = text.replace("r", "w").replace("l", "w").replace("R", "W").replace("L", "W")
        uwu_text = uwu_text.replace("ove", "uv").replace("OVE", "UV")

        faces = [
            "(◕‿◕✿)", "(˶ᵔ ᵕ ᵔ˶)", "(⁄ ⁄>⁄ ▽ ⁄<⁄ ⁄)", "✨", "UwU", "nya~",
            "(⑅˘͈ ᵕ ˘͈ )", "(っ˘з(˘⌣˘ )", "(*^ω^*)"
        ]
        face = random.choice(faces)

        # Stuttering
        words = uwu_text.split()
        for i in range(len(words)):
            if len(words[i]) > 3 and random.random() < 0.25:
                words[i] = f"{words[i][0]}-{words[i]}"

        result = " ".join(words) + f" {face}"
        # The text is member-supplied and sent as plain content, so without this
        # a member could make the bot ping @everyone or a role for them.
        await interaction.response.send_message(
            f"> {result}", allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Social(bot))
