"""The Dev Cat: the Discord half of a pet that lives in the operators' own servers.

``utils/devtoy`` holds the rules and this holds the gateway — the same split
``utils/company.py`` / ``cogs/company.py`` uses, and for the same reason: every
number worth arguing about (how fast a cat gets hungry, how often it may speak
without being asked, whether a message deserves an answer) is testable with no
Discord in sight, while the half below can only be tested by using it.

Four surfaces, one system:

* ``/cat`` — the pet. Adopt one, feed it, pet it, play with it, rename it, see
  the leaderboard, and post the **panel**: a card in a channel whose buttons work
  after a restart.
* ``/bored`` — the solo arcade. One root command opening a hub of games and small
  things to do, with personal records.
* The cat's own cards, on a timer, capped and quiet (see ``utils/devtoy``).
* The cat's answers, when it is mentioned or when a message says one of a few
  words — and only with the Message Content intent, which is a *deployment*
  switch: without it Discord sends this process no message text at all, so the
  listener below finds nothing and everything else still works.

Three of the surfaces fetch something from outside the bot — ``/cat photo`` asks a
picture service, ``/cat fact`` asks a fact API, and ``/cat ask`` (with the cat's
own cards and its answers in chat) asks a language model. All of that lives
behind ``utils/catfeed``, which verifies or refuses, and none of it is required:
the written lines in ``utils/devtoy`` are the fallback for every one of them, so
the cat works with no network at all beyond Discord.

Two gates, and they are different things. The registry ships this system **off**,
because per-server registration is the only mechanism Discord offers for hiding a
slash command in one server, so the toy appears nowhere until it is switched on
in a server of the operator's own. And :func:`utils.devtoy.in_dev_server` refuses
every guild but those two — the ids hardcoded on purpose, in one place, with a
scan test — so a switch flipped in the wrong server is refused rather than obeyed.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import asdict, fields

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import bot_channels, catfeed, db, devtoy, features
from utils.permissions import deny_admin, has_admin
from utils.style import NO_PINGS, VIP_GOLD, display_name, pings, send_card

MODULE = "devcat"

#: How often the cat asks whether it may speak on its own. Cheap by construction:
#: one row read and two clock comparisons, and this process is the same one that
#: serves the web API and the Discord gateway.
TICK_SECONDS = 120

#: The panel's buttons are persistent, so their ids are stable strings rather than
#: a library-generated hash — a re-registered view has to match the message
#: Discord still holds from a previous boot.
BUTTON_PREFIX = "devcat:"


# --------------------------------------------------------------------------- #
# Rows and model
# --------------------------------------------------------------------------- #
def cat_from_row(row: dict | None) -> devtoy.Cat | None:
    """The stored row as the model, ignoring any column the model does not have."""
    if not row:
        return None
    known = {spec.name for spec in fields(devtoy.Cat)}
    return devtoy.Cat(**{key: value for key, value in row.items() if key in known})


def row_from_cat(cat: devtoy.Cat) -> dict:
    """The model as the row a write takes. One mapping, used by every writer."""
    return asdict(cat)


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
class CatActionsView(discord.ui.View):
    """Feed / Pet / Play / Look, on a card that outlives a restart.

    Registered on the bot rather than left to a timeout: the panel sits in a
    channel for weeks, and an unregistered view answers *This interaction failed*
    to the first person who presses a button after a deploy.
    """

    def __init__(self, cog: "DevToy"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Feed", emoji="🍖", style=discord.ButtonStyle.primary,
                       custom_id=f"{BUTTON_PREFIX}feed")
    async def feed(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cog.care_button(interaction, "feed")

    @discord.ui.button(label="Pet", emoji="🐾", style=discord.ButtonStyle.primary,
                       custom_id=f"{BUTTON_PREFIX}pet")
    async def pet(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cog.care_button(interaction, "pet")

    @discord.ui.button(label="Play", emoji="🎾", style=discord.ButtonStyle.success,
                       custom_id=f"{BUTTON_PREFIX}play")
    async def play(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cog.care_button(interaction, "play")

    @discord.ui.button(label="Look", emoji="👀", style=discord.ButtonStyle.secondary,
                       custom_id=f"{BUTTON_PREFIX}status")
    async def status(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cog.status_button(interaction)


class CatSettingsView(discord.ui.View):
    """The four switches, plus the button that proves the ambient half works."""

    def __init__(self, cog: "DevToy"):
        super().__init__(timeout=600)
        self.cog = cog

    async def _toggle(self, interaction: discord.Interaction, setting: str, label: str):
        if not has_admin(interaction):
            return await deny_admin(interaction, f"change {label}")
        current = await self.cog.setting_bool(interaction.guild_id, setting)
        await db.set_setting(
            interaction.guild_id, features.setting_key(MODULE, setting), not current
        )
        embed = await self.cog.settings_embed(interaction.guild_id)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Nudges", emoji="🔔", style=discord.ButtonStyle.secondary,
                       custom_id=f"{BUTTON_PREFIX}set:nudges")
    async def nudges(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._toggle(interaction, "nudges", "the cat's own cards")

    @discord.ui.button(label="Chat", emoji="💬", style=discord.ButtonStyle.secondary,
                       custom_id=f"{BUTTON_PREFIX}set:reactions")
    async def reactions(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._toggle(interaction, "reactions", "the cat's answers in chat")

    @discord.ui.button(label="Ping", emoji="📣", style=discord.ButtonStyle.secondary,
                       custom_id=f"{BUTTON_PREFIX}set:ping")
    async def ping(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._toggle(interaction, "ping", "whether the cat may ping")

    @discord.ui.button(label="Brain", emoji="🧠", style=discord.ButtonStyle.secondary,
                       custom_id=f"{BUTTON_PREFIX}set:brain")
    async def brain(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._toggle(interaction, "brain", "the cat thinking for itself")

    @discord.ui.button(label="Test nudge", emoji="🧪", style=discord.ButtonStyle.primary,
                       custom_id=f"{BUTTON_PREFIX}test")
    async def test(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not has_admin(interaction):
            return await deny_admin(interaction, "send the cat's own card")
        await self.cog.send_nudge(interaction)

    @discord.ui.button(label="Panel here", emoji="📌", style=discord.ButtonStyle.success,
                       custom_id=f"{BUTTON_PREFIX}panel")
    async def panel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not has_admin(interaction):
            return await deny_admin(interaction, "post the cat panel")
        await self.cog.post_panel(interaction, interaction.channel)


class BoredHubView(discord.ui.View):
    """The arcade's front door: a button per game, and three that need no score."""

    def __init__(self, cog: "DevToy"):
        super().__init__(timeout=900)
        self.cog = cog

    async def _game(self, interaction: discord.Interaction, game_id: str):
        await self.cog.start_game(interaction, game_id)

    @discord.ui.button(label="Guess", emoji="🎯", style=discord.ButtonStyle.primary,
                       custom_id=f"{BUTTON_PREFIX}game:guess")
    async def guess(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._game(interaction, "guess")

    @discord.ui.button(label="Reflex", emoji="⚡", style=discord.ButtonStyle.primary,
                       custom_id=f"{BUTTON_PREFIX}game:reaction")
    async def reaction(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._game(interaction, "reaction")

    @discord.ui.button(label="Scramble", emoji="🔤", style=discord.ButtonStyle.primary,
                       custom_id=f"{BUTTON_PREFIX}game:scramble")
    async def scramble(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._game(interaction, "scramble")

    @discord.ui.button(label="Duel", emoji="✊", style=discord.ButtonStyle.primary,
                       custom_id=f"{BUTTON_PREFIX}game:rps")
    async def duel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._game(interaction, "rps")

    @discord.ui.button(label="Surprise", emoji="🎲", style=discord.ButtonStyle.secondary,
                       custom_id=f"{BUTTON_PREFIX}game:random")
    async def surprise(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cog.surprise(interaction)

    @discord.ui.button(label="Riddle", emoji="🤔", style=discord.ButtonStyle.secondary,
                       custom_id=f"{BUTTON_PREFIX}extra:riddle")
    async def riddle(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cog.small_thing(interaction, "riddle")

    @discord.ui.button(label="Joke", emoji="😹", style=discord.ButtonStyle.secondary,
                       custom_id=f"{BUTTON_PREFIX}extra:joke")
    async def joke(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cog.small_thing(interaction, "joke")

    @discord.ui.button(label="Challenge", emoji="📝", style=discord.ButtonStyle.secondary,
                       custom_id=f"{BUTTON_PREFIX}extra:challenge")
    async def challenge(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cog.small_thing(interaction, "challenge")


class GuessModal(discord.ui.Modal, title="Guess my number"):
    guess = discord.ui.TextInput(label="A number from 1 to 100", max_length=3)

    def __init__(self, view: "GuessView"):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        await self.view_ref.submit(interaction, str(self.guess.value))


class GuessView(discord.ui.View):
    """One number, guessed one at a time; the card is the scoreboard."""

    def __init__(self, cog: "DevToy", member_id: int, secret: int):
        super().__init__(timeout=600)
        self.cog = cog
        self.member_id = member_id
        self.secret = secret
        self.tries = 0
        self.done = False

    @discord.ui.button(label="Guess", emoji="🔢", style=discord.ButtonStyle.primary)
    async def guess(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.member_id:
            return await interaction.response.send_message(
                "This one is somebody else's. Start your own with `/bored`.", ephemeral=True
            )
        if self.done:
            return await interaction.response.send_message("That one is over. 🐾", ephemeral=True)
        await interaction.response.send_modal(GuessModal(self))

    async def submit(self, interaction: discord.Interaction, raw: str):
        if interaction.user.id != self.member_id:
            return await interaction.response.send_message("Not your game. 🐾", ephemeral=True)
        try:
            guess = int(str(raw).strip())
        except (TypeError, ValueError):
            return await interaction.response.send_message(
                "A whole number between 1 and 100, please.", ephemeral=True
            )
        if not (devtoy.GUESS_MIN <= guess <= devtoy.GUESS_MAX):
            return await interaction.response.send_message(
                f"{devtoy.GUESS_MIN}–{devtoy.GUESS_MAX} only.", ephemeral=True
            )
        self.tries += 1
        verdict = devtoy.guess_verdict(self.secret, guess)
        if verdict == "hit":
            self.done = True
            for item in self.children:
                item.disabled = True
            fresh = await self.cog.record_game(interaction, "guess", self.tries)
            body = f"{devtoy.guess_comment(self.tries)}\n{fresh}"
            return await interaction.response.edit_message(
                embed=self.cog.embed("🎯 Got it", body), view=self
            )
        hint = "higher ⬆️" if verdict == "higher" else "lower ⬇️"
        body = (
            f"**{guess}** is not it — go **{hint}**.\n"
            f"Guesses so far: **{self.tries}** · par is {devtoy.GUESS_PAR}."
        )
        await interaction.response.edit_message(embed=self.cog.embed("🎯 Guess my number", body), view=self)


class ScrambleModal(discord.ui.Modal, title="Unscramble it"):
    answer = discord.ui.TextInput(label="The word", max_length=32)

    def __init__(self, view: "ScrambleView"):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        await self.view_ref.submit(interaction, str(self.answer.value))


class ScrambleView(discord.ui.View):
    def __init__(self, cog: "DevToy", member_id: int, answer: str):
        super().__init__(timeout=600)
        self.cog = cog
        self.member_id = member_id
        self.answer = answer
        self.started = time.time()
        self.done = False

    @discord.ui.button(label="Answer", emoji="✍️", style=discord.ButtonStyle.primary)
    async def answer_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.member_id:
            return await interaction.response.send_message("Somebody else's game. 🐾", ephemeral=True)
        if self.done:
            return await interaction.response.send_message("Already solved. 🐾", ephemeral=True)
        await interaction.response.send_modal(ScrambleModal(self))

    async def submit(self, interaction: discord.Interaction, raw: str):
        if interaction.user.id != self.member_id:
            return await interaction.response.send_message("Not your game. 🐾", ephemeral=True)
        took = max(0.1, time.time() - self.started)
        if str(raw).strip().lower() != self.answer.lower():
            return await interaction.response.send_message(
                "Not it. Try again — the clock is still running.", ephemeral=True
            )
        self.done = True
        for item in self.children:
            item.disabled = True
        fresh = await self.cog.record_game(interaction, "scramble", round(took, 1))
        body = f"The word was **{self.answer}**, in **{took:.1f}s**.\n{fresh}"
        await interaction.response.edit_message(embed=self.cog.embed("🔤 Solved", body), view=self)


class ReflexView(discord.ui.View):
    """One button that only exists once the paw appears — the clock starts then."""

    def __init__(self, cog: "DevToy", member_id: int, appeared: float):
        super().__init__(timeout=120)
        self.cog = cog
        self.member_id = member_id
        self.appeared = appeared

    @discord.ui.button(label="TAP", emoji="🐾", style=discord.ButtonStyle.danger)
    async def tap(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.member_id:
            return await interaction.response.send_message("Not your paw. 🐾", ephemeral=True)
        ms = max(1.0, (time.time() - self.appeared) * 1000.0)
        button.disabled = True
        fresh = await self.cog.record_game(interaction, "reaction", round(ms, 1))
        body = f"**{ms:.0f} ms** — {devtoy.reflex_comment(ms)}\n{fresh}"
        await interaction.response.edit_message(embed=self.cog.embed("⚡ Paw reflex", body), view=self)


class DuelView(discord.ui.View):
    """Best of five, and the cat's move is chosen the moment you move."""

    def __init__(self, cog: "DevToy", member_id: int):
        super().__init__(timeout=600)
        self.cog = cog
        self.member_id = member_id
        self.you = 0
        self.cat = 0
        self.done = False

    async def _round(self, interaction: discord.Interaction, mine: int):
        if interaction.user.id != self.member_id:
            return await interaction.response.send_message("Somebody else's duel. 🐾", ephemeral=True)
        if self.done:
            return await interaction.response.send_message("The duel is over. 🐾", ephemeral=True)
        theirs = self.cog.rng.randrange(3)
        verdict = devtoy.rps_verdict(mine, theirs)
        if verdict == "you":
            self.you += 1
        elif verdict == "cat":
            self.cat += 1
        line = devtoy.duel_line(verdict, self.cog.rng)
        board = (
            f"You {devtoy.RPS_MOVES[mine]} · Cat {devtoy.RPS_MOVES[theirs]}\n"
            f"{line}\n\n**You {self.you} — {self.cat} Cat**"
        )
        if self.you >= devtoy.DUEL_WINS_NEEDED or self.cat >= devtoy.DUEL_WINS_NEEDED:
            self.done = True
            for item in self.children:
                item.disabled = True
            fresh = await self.cog.record_game(interaction, "rps", self.you)
            winner = "You take the duel." if self.you > self.cat else "The cat takes it."
            board = f"{board}\n\n**{winner}**\n{fresh}"
        await interaction.response.edit_message(embed=self.cog.embed("✊ Cat duel", board), view=self)

    @discord.ui.button(label="Rock", emoji="✊", style=discord.ButtonStyle.primary)
    async def rock(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._round(interaction, 0)

    @discord.ui.button(label="Paper", emoji="✋", style=discord.ButtonStyle.primary)
    async def paper(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._round(interaction, 1)

    @discord.ui.button(label="Scissors", emoji="✌️", style=discord.ButtonStyle.primary)
    async def scissors(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._round(interaction, 2)


# --------------------------------------------------------------------------- #
# The cog
# --------------------------------------------------------------------------- #
class DevToy(commands.Cog):
    """A cat in the bot's own servers, for the operator, and refused everywhere else."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.rng = random.Random()
        #: Reaction and reply clocks, per member. Memory rather than the database on
        #: purpose: these exist to keep the cat from talking over somebody, and a
        #: restart starting with a clean slate is the harmless direction to fail in.
        self._last_react: dict[int, float] = {}
        self._last_reply: dict[int, float] = {}
        # The panel is the only **persistent** view here, and that is a Discord rule
        # rather than a preference: ``add_view`` refuses anything with a timeout, so
        # the settings and hub cards (both transient, both re-created per
        # invocation) are held rather than registered. The panel sits in a channel
        # for weeks, which is exactly why it has to survive a deploy.
        self.panel_view = CatActionsView(self)
        self.settings_view = CatSettingsView(self)
        self.hub_view = BoredHubView(self)
        adder = getattr(bot, "add_view", None)
        if callable(adder):
            adder(self.panel_view)
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # instantiated outside a loop (scripts, tests)
            pass
        else:
            self.ambient.start()

    def cog_unload(self):
        self.ambient.cancel()

    # -- gates --------------------------------------------------------------- #
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Refuse every guild but the developer's, and any server with the system off."""
        return await self.allow(interaction)

    async def allow(self, interaction: discord.Interaction) -> bool:
        """The two gates, as one answer, with the refusal already sent.

        The **module** is asked first, because that is the gate a server can do
        something about and the one Discord's own hiding rule stands in for: a
        refused command whose picker entry was removed has to say why it is gone,
        or the server reads the refusal as a bug. The guild gate is the hard one
        and comes second — it is the one nobody can switch on.
        """
        guild_id = getattr(interaction, "guild_id", None)
        if guild_id is None:
            guild = getattr(interaction, "guild", None)
            guild_id = getattr(guild, "id", None)
        if not self.bot.is_module_enabled(guild_id, MODULE):
            await self._refuse(
                interaction,
                "⚠️ The **Dev Cat** system is **turned off** on this server. "
                "An administrator can turn it on from this server's dashboard.",
            )
            return False
        if not devtoy.in_dev_server(guild_id):
            await self._refuse(
                interaction,
                "🐈 The cat lives in the bot's own servers only — this is not one "
                "of them. Nothing here happens anywhere else, by design.",
            )
            return False
        return True

    @staticmethod
    async def _refuse(interaction: discord.Interaction, text: str) -> None:
        """Say no, whichever of the two ways is still open.

        ``is_done`` is read with ``getattr`` rather than assumed: the fake
        interactions other suites drive these cogs with (``tests/test_command_scope``
        is the one that matters) carry a response object with no such method, and a
        gate that raises while refusing is a gate that fails open.
        """
        response = getattr(interaction, "response", None)
        is_done = getattr(response, "is_done", None)
        try:
            if callable(is_done) and is_done():
                await interaction.followup.send(text, ephemeral=True)
            elif response is not None:
                await response.send_message(text, ephemeral=True)
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            pass

    # -- storage ------------------------------------------------------------- #
    async def _load(self, guild_id: int, user_id: int) -> devtoy.Cat | None:
        return cat_from_row(await db.devcat_pet(guild_id, user_id))

    async def _save(self, guild_id: int, user_id: int, cat: devtoy.Cat) -> None:
        await db.save_devcat_pet(guild_id, user_id, row_from_cat(cat))

    async def setting(self, guild_id: int, setting_id: str, default=None):
        if guild_id is None:
            return default
        return await db.get_setting(guild_id, features.setting_key(MODULE, setting_id), default)

    # -- the cat's brain ------------------------------------------------------ #
    async def brain_on(self, guild_id: int) -> bool:
        """Whether the cat may ask a model what to say, on this server."""
        return await self.setting_bool(guild_id, "brain", default=True)

    async def think(self, guild_id: int, messages: list[dict], *, temperature: float = 0.8) -> str | None:
        """What the cat thought of, or ``None`` to use a line written for it.

        The switch, the call and the failure are one decision rather than three
        checkpoints at each call site: a server with the brain off makes **no**
        outbound request at all, and a provider that is down, slow or talking
        nonsense ends in ``None`` — which every caller can answer with the pool in
        ``utils/devtoy``. Nothing here may raise: this sits in front of cards and
        of a message listener.
        """
        if not await self.brain_on(guild_id):
            return None
        try:
            return await catfeed.say(messages, temperature=temperature)
        except Exception as exc:  # noqa: BLE001 — a model must never break a card
            print(f"[DEVCAT] brain call failed: {exc!r}", flush=True)
            return None

    async def setting_bool(self, guild_id: int, setting_id: str, default: bool = True) -> bool:
        spec = features.get_setting(MODULE, setting_id)
        fallback = default if spec is None else bool(spec.default)
        raw = await self.setting(guild_id, setting_id, None)
        if raw is None:
            return fallback
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in features.BOOL_TRUE:
            return True
        if text in features.BOOL_FALSE:
            return False
        return fallback

    # -- cards --------------------------------------------------------------- #
    @staticmethod
    def embed(title: str, body: str, *, footer: str = "") -> discord.Embed:
        embed = discord.Embed(title=title, description=body, color=VIP_GOLD)
        embed.set_footer(text=footer or "🐈 DaCat VIP · Dev Cat")
        return embed

    # -- care ---------------------------------------------------------------- #
    async def care(self, interaction: discord.Interaction, action_id: str) -> None:
        """One care action from a command, worked through the shared rules."""
        guild_id, user_id = interaction.guild_id, interaction.user.id
        cat = await self._load(guild_id, user_id)
        if cat is None:
            return await send_card(
                interaction,
                embed=self.embed(
                    "🐈 No cat here yet",
                    "Adopt one first — `/cat adopt` — and then this works.",
                ),
                ephemeral=True,
            )
        result = devtoy.care(cat, action_id, time.time(), rng=self.rng)
        if result.ok:
            await self._save(guild_id, user_id, result.cat)
        await send_card(
            interaction,
            embed=self._care_embed(result),
            view=self.panel_view,
            ephemeral=True,
        )

    def _care_embed(self, result: devtoy.CareResult) -> discord.Embed:
        title = f"{devtoy.face(result.cat)} {result.headline}"
        body = result.detail
        if result.ok:
            body = f"{body}\n\n{devtoy.panel_body(result.cat, time.time())}"
        return self.embed(title, body)

    async def care_button(self, interaction: discord.Interaction, action_id: str) -> None:
        """A panel button: answers the presser privately and refreshes the panel."""
        if not await self.allow(interaction):
            return
        await self.care(interaction, action_id)
        await self._refresh_panel(interaction)

    async def _refresh_panel(self, interaction: discord.Interaction) -> None:
        """Re-render the panel card the button was pressed on, if it is one."""
        message = getattr(interaction, "message", None)
        if message is None or not hasattr(message, "edit"):
            return
        cat = await self._load(interaction.guild_id, interaction.user.id)
        if cat is None:
            return
        try:
            await message.edit(
                embed=self.embed("🐈 Dev Cat", devtoy.panel_body(cat, time.time())),
                view=self.panel_view,
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def status_button(self, interaction: discord.Interaction) -> None:
        if not await self.allow(interaction):
            return
        await self.show_status(interaction, interaction.user)

    async def show_status(self, interaction: discord.Interaction, member) -> None:
        guild_id = interaction.guild_id
        cat = await self._load(guild_id, getattr(member, "id", 0))
        who = "You" if getattr(member, "id", 0) == interaction.user.id else "They"
        if cat is None:
            return await send_card(
                interaction,
                embed=self.embed("🐈 No cat", f"{who} have not adopted a cat here yet."),
                ephemeral=True,
            )
        now = time.time()
        rows = "\n".join(f"**{name}** · {value}" for name, value in devtoy.status_fields(cat, now))
        body = f"{rows}\n\n-# Care actions are rate-limited; the cat is a clock, not a job."
        await send_card(
            interaction,
            embed=self.embed(f"{devtoy.face(cat)} {cat.name or devtoy.DEFAULT_NAME}", body),
            view=self.panel_view,
            ephemeral=True,
        )

    # -- the arcade ---------------------------------------------------------- #
    async def record_game(self, interaction: discord.Interaction, game_id: str, value: float) -> str:
        """Store a finished game and say whether it beat the old record."""
        guild_id, user_id = interaction.guild_id, interaction.user.id
        existing = await db.devcat_record(guild_id, user_id, game_id)
        previous = existing["best"] if existing else None
        plays = (existing["plays"] if existing else 0) + 1
        better = devtoy.record_better(game_id, value, previous)
        stored = float(value) if better else float(previous or 0.0)
        await db.save_devcat_record(guild_id, user_id, game_id, stored, plays=plays)
        cat = await self._load(guild_id, user_id)
        if cat is not None:
            await self._save(guild_id, user_id, devtoy.Cat(**{**asdict(cat), "games": cat.games + 1}))
        if better and previous:
            return f"🏅 New record — {devtoy.record_text(game_id, value)} (was {devtoy.record_text(game_id, previous)})."
        if better:
            return f"🏅 First record — {devtoy.record_text(game_id, value)}."
        return f"Record stands at {devtoy.record_text(game_id, previous)}."

    async def start_game(self, interaction: discord.Interaction, game_id: str) -> None:
        if not await self.allow(interaction):
            return
        if game_id == "random":
            game_id = self.rng.choice(tuple(game.id for game in devtoy.GAMES))
        member_id = interaction.user.id
        if game_id == "guess":
            return await interaction.response.send_message(
                embed=self.embed(
                    "🎯 Guess my number",
                    f"I have one between {devtoy.GUESS_MIN} and {devtoy.GUESS_MAX}. "
                    f"Par is {devtoy.GUESS_PAR} guesses.",
                ),
                view=GuessView(self, member_id, devtoy.guess_secret(self.rng)),
            )
        if game_id == "scramble":
            answer, scrambled = devtoy.scramble_round(self.rng)
            return await interaction.response.send_message(
                embed=self.embed("🔤 Word scramble", f"Unscramble: **`{scrambled}`**"),
                view=ScrambleView(self, member_id, answer),
            )
        if game_id == "rps":
            return await interaction.response.send_message(
                embed=self.embed(
                    "✊ Cat duel", f"First to {devtoy.DUEL_WINS_NEEDED} wins. I never lose, obviously."
                ),
                view=DuelView(self, member_id),
            )
        if game_id == "reaction":
            await interaction.response.send_message(
                embed=self.embed("⚡ Paw reflex", "Wait for the paw. Do not anticipate it.")
            )
            return await self._arm_reflex(interaction, member_id)
        return await self.small_thing(interaction, "joke")

    async def _arm_reflex(self, interaction: discord.Interaction, member_id: int) -> None:
        """Show the paw after a random wait, and only then start the clock.

        A reflex game that can be won by pressing early is not a reflex game, which
        is why the button does not exist until the delay has passed and why the
        wait is drawn from a range rather than being a fixed number.
        """
        try:
            message = await interaction.original_response()
        except discord.HTTPException:
            return
        await asyncio.sleep(self.rng.uniform(1.5, 4.5))
        view = ReflexView(self, member_id, time.time())
        try:
            await message.edit(
                embed=self.embed("⚡ Paw reflex", "**NOW.** 🐾"), view=view
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def small_thing(self, interaction: discord.Interaction, kind: str) -> None:
        """A riddle, a joke or a challenge — the arcade's low-effort answers."""
        if not await self.allow(interaction):
            return
        if kind == "riddle":
            question, answer = devtoy.pick(devtoy.RIDDLES, self.rng)
            body = f"> {question}\n\n-# Answer: ||{answer}||"
            title = "🤔 A riddle"
        elif kind == "challenge":
            body = f"> {devtoy.pick(devtoy.CHALLENGES, self.rng)}"
            title = "📝 One challenge"
        else:
            body = devtoy.pick(devtoy.JOKES, self.rng)
            title = "😹 Cat joke"
        await send_card(interaction, embed=self.embed(title, body), ephemeral=True)

    async def surprise(self, interaction: discord.Interaction) -> None:
        if not await self.allow(interaction):
            return
        rolled = devtoy.roll_activity(self.rng)
        if rolled in devtoy.EXTRA_ACTIVITIES:
            return await self.small_thing(interaction, rolled)
        await self.start_game(interaction, rolled)

    # -- the cat's own cards -------------------------------------------------- #
    async def ambient_channel(self, guild) -> object | None:
        """Where the cat's own cards go: the setting, then the hardcoded room, then
        the server's bound bot channel, then nothing.

        Falling through to *nothing* matters: a card posted somewhere nobody reads
        still spends one of the day's three, and a toy that spends its allowance on
        a deleted channel is a toy that never speaks.
        """
        configured = await self.setting(guild.id, "home_channel", "")
        if configured:
            try:
                channel = guild.get_channel(int(configured))
            except (TypeError, ValueError):
                channel = None
            if channel is not None:
                return channel
        channel = guild.get_channel(bot_channels.DEV_CHAT_CHANNEL_ID)
        if channel is not None:
            return channel
        bound = await bot_channels.bound(guild.id, "commands")
        if bound:
            return guild.get_channel(bound)
        return None

    async def send_nudge(self, interaction: discord.Interaction) -> None:
        """One card, asked for (the settings card's 🧪 button).

        Asked-for means the allowance is **not** spent: the caps exist to bound what
        the *cat* decides to do, and a card somebody pressed a button for is a
        person speaking, not the cat. ``/bot push`` makes the same distinction about
        the update window for the same reason.
        """
        guild = interaction.guild
        channel = await self.ambient_channel(guild)
        if channel is None:
            return await send_card(
                interaction,
                embed=self.embed(
                    "🐈 Nowhere to talk",
                    "I have no channel here yet. Set one on this card, or bind a "
                    "`#bot-cmd`-style room for the bot.",
                ),
                ephemeral=True,
            )
        row = await self._resident_row(guild.id)
        if row is None:
            return await send_card(
                interaction,
                embed=self.embed("🐈 No cat yet", "Adopt one first — the card is theirs."),
                ephemeral=True,
            )
        kind = devtoy.pick(devtoy.NUDGE_KINDS, self.rng)
        delivered = await self._deliver_card(guild, channel, row, kind)
        where = getattr(channel, "mention", "#channel")
        text = f"Sent one to {where}." if delivered else (
            f"I could not post in {where} — check my permissions there."
        )
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def _resident_row(self, guild_id: int) -> dict | None:
        """Whose cat speaks for the server: the best-cared-for one's row.

        The cat is per member, and an unprompted card is the *server's*, so it has
        to be somebody's cat. The most cared-for one is the honest choice — it is
        the cat that lives here most — and its owner is who the card may ping.
        """
        top = await db.devcat_leaderboard(guild_id, limit=1)
        return top[0] if top else None

    async def _deliver_card(self, guild, channel, row: dict, kind: str) -> bool:
        """Send one of the cat's own cards. Returns whether it landed.

        The ping switch is what makes the card *about* somebody: with it on, the
        card mentions the cat's own person, which is the one mention a card nobody
        asked for is allowed to carry. With it off — the default — the card is sent
        with :data:`NO_PINGS` and the words alone are mention-free by construction
        (asserted in ``tests/test_devtoy.py`` over every line the rules can pick).
        """
        cat = cat_from_row(row)
        if cat is None:
            return False
        now = time.time()
        # The cat is allowed to *think of* its own opener when the brain is on.
        # The written one is what it says when that is off, unreachable, slow, or
        # refused — so a card always goes out, and the card's shape never changes.
        thought = await self.think(guild.id, devtoy.ambient_messages(kind, cat, now))
        title, body = devtoy.ambient_text(kind, cat, now, rng=self.rng, instead=thought or "")
        ping = await self.setting_bool(guild.id, "ping", default=False)
        owner_id = int(row.get("user_id") or 0)
        allowed = None
        if ping and owner_id:
            body = f"<@{owner_id}>\n{body}"
            allowed = pings(owner_id)
        try:
            await send_card(
                channel,
                embed=self.embed(title, body, footer="🐈 the cat, unprompted"),
                allowed_mentions=allowed,
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"[DEVCAT] could not post a card in {getattr(channel, 'id', '?')}: {exc}", flush=True)
            return False
        return True

    @tasks.loop(seconds=TICK_SECONDS)
    async def ambient(self):
        try:
            await self._ambient_once()
        except Exception as exc:  # noqa: BLE001 — a toy must never kill the loop
            print(f"[DEVCAT] ambient tick failed: {exc!r}", flush=True)

    async def _ambient_once(self) -> None:
        guild = self.bot.get_guild(bot_channels.DEV_SERVER_GUILD_ID)
        if guild is None:
            return
        if not self.bot.is_module_enabled(guild.id, MODULE):
            return
        if not await self.setting_bool(guild.id, "nudges", default=True):
            return
        row = await db.devcat_ambient(guild.id)
        now = time.time()
        state = devtoy.AmbientState(
            last_nudge=float(row.get("last_nudge", 0.0)),
            nudge_day=str(row.get("nudge_day", "")),
            nudge_count=int(row.get("nudge_count", 0)),
        )
        decision = devtoy.nudge_due(state, now, enabled=True, rng=self.rng)
        if not decision.ok:
            return
        channel = await self.ambient_channel(guild)
        if channel is None:
            return
        row = await self._resident_row(guild.id)
        if row is None:
            return
        if not await self._deliver_card(guild, channel, row, decision.kind):
            # A card that did not land has not been said, so the allowance is not
            # spent: a channel that refuses for a day must not cost the three cards
            # the cat would otherwise have used tomorrow.
            return
        after = devtoy.mark_nudge(state, now)
        await db.save_devcat_ambient(
            guild.id,
            last_nudge=after.last_nudge,
            nudge_day=after.nudge_day,
            nudge_count=after.nudge_count,
        )

    # -- the cat's answers ---------------------------------------------------- #
    async def chat_gate(self, guild_id: int) -> str | None:
        """Why the cat will not read this server, or ``None`` when it will.

        The chat half's four refusals as four sentences, in one place, because the
        listener and the operator's rehearsal both ask this and a second copy of
        them would be a second answer. The order is the listener's: the intent is
        whether Discord sends any text *at all*, the module is the switch a server
        can flip, and the guild gate is the one nobody can.
        """
        if not getattr(self.bot.intents, "message_content", False):
            # Discord sends no text at all without the privileged intent, so there
            # is nothing to read and no reason to guess.
            return (
                "the Message Content intent is off, so Discord sends no text at "
                "all (set ENABLE_MESSAGE_CONTENT_INTENT=1 on this host)"
            )
        if not devtoy.in_dev_server(guild_id):
            return "the cat lives in the bot's own servers only, and this is not one of them"
        if not self.bot.is_module_enabled(guild_id, MODULE):
            return "the Dev Cat system is switched off for this server"
        if not await self.setting_bool(guild_id, "reactions", default=True):
            return '"Answers in chat" is switched off for this server'
        return None

    async def chat_decision_for(
        self,
        guild_id: int,
        user_id: int,
        content: str,
        *,
        mentioned: bool = False,
        now: float | None = None,
    ) -> tuple[devtoy.ChatDecision, devtoy.Cat | None]:
        """What the cat would do with one message, moving neither clock.

        Read-only on purpose. ``devtoy.chat_decision`` is a pure function of the
        text and the two clocks, and this is its one caller in a process that has
        both — so what a rehearsal reports is what a member gets, and a rehearsal
        cannot spend the member's allowance. The clock is moved by the caller that
        sends, which is also the only place that knows whether anything was sent.
        """
        moment = time.time() if now is None else now
        cat = await self._load(guild_id, user_id)
        decision = devtoy.chat_decision(
            content,
            mentioned=mentioned,
            cat_name=cat.name if cat else "",
            now=moment,
            last_react=self._last_react.get(user_id, 0.0),
            last_reply=self._last_reply.get(user_id, 0.0),
            rng=self.rng,
        )
        return decision, cat

    async def phrase_reply(
        self,
        guild_id: int,
        written: str,
        content: str,
        cat: devtoy.Cat | None,
        *,
        now: float | None = None,
    ) -> tuple[str, str]:
        """The sentence the cat would say, and which half of it wrote that.

        The model's when it answers, the line written for it otherwise — two halves
        that already existed, kept here so that a card, a rehearsal and the
        listener cannot disagree about which one spoke. A host with no key is a
        host whose sentences come from ``written``, which is not the same thing as
        a host where the cat is silent, and the second value is what says so.
        """
        moment = time.time() if now is None else now
        thought = await self.think(
            guild_id, devtoy.reply_messages(content, cat, moment)
        )
        if thought:
            return thought, "model"
        return written, "written"

    async def rehearse(
        self,
        guild_id: int,
        user_id: int,
        content: str,
        *,
        mentioned: bool = False,
    ) -> dict:
        """What the cat would say to one message, as a report, having said nothing.

        Built for the operator's own door (``/api/devcat-simulate``) because the
        chat half's last hop cannot be asked in any other way: the listener ignores
        bots and webhooks by design, so exercising it needs a person at a keyboard
        — which left *"is the cat answering in there"* with no answer a terminal
        could get, on the machine that had just been deployed. Nothing is sent and
        no clock moves; every field is a fact about the decision rather than an
        opinion about it — the gate that refused, the rule that matched, the
        sentence, and which half produced it.
        """
        report: dict = {
            "gate": None,
            "matched": "",
            "reply": None,
            "react": None,
            "source": None,
            "cat": None,
            "would": "nothing",
        }
        gate = await self.chat_gate(guild_id)
        if gate is not None:
            report["gate"] = gate
            return report
        decision, cat = await self.chat_decision_for(
            guild_id, user_id, content, mentioned=mentioned
        )
        report["matched"] = decision.matched
        report["cat"] = cat.name if cat else None
        if decision.reply:
            said, source = await self.phrase_reply(
                guild_id, decision.reply, content, cat
            )
            report.update(reply=said, source=source, would="reply")
            return report
        if decision.react:
            report.update(react=decision.react, would="react")
        return report

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """React to, or answer, one message in the developer's server.

        Every refusal is checked *before* anything is sent, and the whole thing is
        wrapped: this runs on every message the bot can see, and a listener that
        raises is a listener Discord removes. The refusals are ``chat_gate``'s and
        the decision is ``chat_decision_for``'s, so the operator's rehearsal runs
        exactly this path — the sender is the only part that differs.
        """
        try:
            author = getattr(message, "author", None)
            if author is None or getattr(author, "bot", False):
                return
            if getattr(message, "webhook_id", None):
                return
            guild = getattr(message, "guild", None)
            if guild is None:
                return
            content = getattr(message, "content", "") or ""
            if not content.strip():
                return
            if await self.chat_gate(guild.id) is not None:
                return
            now = time.time()
            me = getattr(self.bot, "user", None)
            mentioned = bool(me) and any(
                getattr(user, "id", None) == getattr(me, "id", None)
                for user in (getattr(message, "mentions", None) or [])
            )
            decision, cat = await self.chat_decision_for(
                guild.id, author.id, content, mentioned=mentioned, now=now
            )
            if decision.reply:
                # The clock moves *before* the model is asked, not after: a
                # generation takes seconds, and a member who typed twice while it
                # was running must not get two answers for one allowance.
                self._last_reply[author.id] = now
                said, _source = await self.phrase_reply(
                    guild.id, decision.reply, content, cat, now=now
                )
                await message.reply(
                    said, mention_author=False, allowed_mentions=NO_PINGS
                )
                return
            if decision.react:
                self._last_react[author.id] = now
                await message.add_reaction(decision.react)
        except Exception as exc:  # noqa: BLE001 — see the docstring
            print(f"[DEVCAT] on_message failed: {exc!r}", flush=True)

    # ------------------------------------------------------------------ #
    # /cat
    # ------------------------------------------------------------------ #
    cat = app_commands.Group(
        name="cat",
        description="🐈 The Dev Cat — a pet that lives in the bot's own servers (VIP).",
    )

    @cat.command(name="adopt", description="Adopt your own cat (one each).")
    @app_commands.describe(name="What to call it (anything rude is refused, not escaped).")
    async def cat_adopt(self, interaction: discord.Interaction, name: str = ""):
        cat = await self._load(interaction.guild_id, interaction.user.id)
        if cat is not None:
            return await self.show_status(interaction, interaction.user)
        now = time.time()
        fresh = devtoy.adopt(now, name)
        await self._save(interaction.guild_id, interaction.user.id, fresh)
        body = (
            f"**{fresh.name}** has moved in. 🐾\n\n{devtoy.panel_body(fresh, now)}\n\n"
            "Feed them, pet them, play with them — or type `/bored` when there is nothing else."
        )
        await send_card(
            interaction,
            embed=self.embed("🐈 A cat, adopted", body),
            view=self.panel_view,
        )

    @cat.command(name="status", description="How your cat is doing (or somebody else's).")
    @app_commands.describe(member="Whose cat to look at. Yours by default.")
    async def cat_status(self, interaction: discord.Interaction, member: discord.Member | None = None):
        await self.show_status(interaction, member or interaction.user)

    @cat.command(name="feed", description="Feed your cat.")
    async def cat_feed(self, interaction: discord.Interaction):
        await self.care(interaction, "feed")

    @cat.command(name="pet", description="Pet your cat.")
    async def cat_pet(self, interaction: discord.Interaction):
        await self.care(interaction, "pet")

    @cat.command(name="play", description="Play with your cat (costs them energy).")
    async def cat_play(self, interaction: discord.Interaction):
        await self.care(interaction, "play")

    @cat.command(name="rename", description="Rename your cat.")
    @app_commands.describe(name="The new name.")
    async def cat_rename(self, interaction: discord.Interaction, name: str):
        cat = await self._load(interaction.guild_id, interaction.user.id)
        if cat is None:
            return await send_card(
                interaction, embed=self.embed("🐈 No cat yet", "`/cat adopt` first."), ephemeral=True
            )
        cleaned = devtoy.clean_name(name)
        renamed = devtoy.Cat(**{**asdict(cat), "name": cleaned})
        await self._save(interaction.guild_id, interaction.user.id, renamed)
        note = (
            "\n-# That one would not survive a card — mentions, links and code fences "
            "are refused rather than escaped, so the cat kept the name it has."
            if devtoy.name_refused(name) else ""
        )
        await send_card(
            interaction,
            embed=self.embed("🐈 Renamed", f"Now answering to **{cleaned}**.{note}"),
            ephemeral=True,
        )

    @cat.command(name="top", description="The best-cared-for cats in this server.")
    async def cat_top(self, interaction: discord.Interaction):
        rows = await db.devcat_leaderboard(interaction.guild_id, limit=10)
        if not rows:
            return await send_card(
                interaction,
                embed=self.embed("🐈 No cats yet", "`/cat adopt` starts the list."),
                ephemeral=True,
            )
        lines = []
        for index, row in enumerate(rows, start=1):
            cat = cat_from_row(row)
            if cat is None:
                continue
            who = await display_name(interaction.guild, row["user_id"])
            lines.append(
                f"`{index}.` {devtoy.face(cat)} **{cat.name or devtoy.DEFAULT_NAME}** — "
                f"{who} · level {devtoy.care_level(cat)} · {cat.care_days}d streak"
            )
        await send_card(
            interaction,
            embed=self.embed("🐈 Cats of this server", "\n".join(lines)),
            ephemeral=True,
        )

    # -- what the cat fetches ------------------------------------------------ #
    @cat.command(name="photo", description="A real cat photograph, fetched just now.")
    async def cat_photo(self, interaction: discord.Interaction):
        """One picture, posted where it was asked for rather than privately.

        The only `/cat` command that answers in public, and deliberately: a
        photograph is a thing to show people, and it is the one card here that
        needs nobody to read a word of it.
        """
        if not await self.allow(interaction):
            return
        # Asked for *before* the fetch: a picture service can take several seconds
        # and Discord gives a command three, so the alternative to this line is a
        # card that arrives as "the application did not respond".
        await interaction.response.defer()
        url = await catfeed.photo()
        if not url:
            return await send_card(
                interaction,
                embed=self.embed(
                    "📷 No cat answered",
                    "Every picture service I asked is quiet, or answered with "
                    "something that was not a picture. Nothing was drawn rather "
                    "than a broken frame — try again in a minute.",
                ),
                ephemeral=True,
            )
        cat = await self._load(interaction.guild_id, interaction.user.id)
        now = time.time()
        said = await self.think(interaction.guild_id, devtoy.caption_messages(cat, now))
        caption = said or devtoy.pick(devtoy.PHOTO_CAPTIONS, self.rng).format(
            name=(cat.name if cat else devtoy.DEFAULT_NAME)
        )
        embed = self.embed("📷 A cat", caption, footer="🐈 fetched, not hardcoded")
        embed.set_image(url=url)
        await send_card(interaction, embed=embed)

    @cat.command(name="fact", description="One real cat fact, looked up rather than remembered.")
    async def cat_fact(self, interaction: discord.Interaction):
        """A fact from a public API, and the card says which one it came from.

        The footer is the honest half of this: a fetched fact and one of the
        fortnight written into ``utils/devtoy`` are both true, and only one of
        them was looked up, so the card does not let the reader guess which.
        """
        if not await self.allow(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        text = await catfeed.fact()
        source = "catfact.ninja" if text else "the cat's own notes"
        if not text:
            text = devtoy.pick(devtoy.FACTS, self.rng)
        await send_card(
            interaction,
            embed=self.embed("🐈 A cat fact", text, footer=f"🐈 {source}"),
            ephemeral=True,
        )

    @cat.command(name="ask", description="Ask the cat something. It is a cat, so: good luck.")
    @app_commands.describe(question="What to ask it.")
    async def cat_ask(self, interaction: discord.Interaction, question: str):
        """The one command whose whole answer is written by a model.

        Slow on purpose and slow by nature — a generation is not a lookup — which
        is why it defers first and is private: an answer that arrives in twelve
        seconds is a fine card and a terrible public interruption.
        """
        if not await self.allow(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        cat = await self._load(interaction.guild_id, interaction.user.id)
        now = time.time()
        said = await self.think(interaction.guild_id, devtoy.ask_messages(question, cat, now))
        if said:
            title, footer = "🐈 The cat considers it", "🐈 the cat, thinking"
        else:
            said = devtoy.pick(devtoy.ASK_FALLBACKS, self.rng)
            title, footer = "🐈 The cat declines", "🐈 the cat, not thinking about it"
        # The question is quoted back, and it is somebody else's text going into a
        # card: it passes through the same cleaner a model's answer does, so an
        # ``@everyone`` typed into the question cannot be posted by the cat.
        asked = catfeed.clean(question, limit=200) or "something i have already forgotten"
        await send_card(
            interaction,
            embed=self.embed(title, f"> {asked}\n\n{said}", footer=footer),
            ephemeral=True,
        )

    @cat.command(name="panel", description="Post the cat panel here (buttons that keep working).")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(channel="Where to post it. Here by default.")
    async def cat_panel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        await self.post_panel(interaction, channel or interaction.channel)

    async def post_panel(self, interaction: discord.Interaction, channel) -> None:
        if not await self.allow(interaction):
            return
        cat = await self._load(interaction.guild_id, interaction.user.id)
        if cat is None:
            return await send_card(
                interaction, embed=self.embed("🐈 No cat yet", "`/cat adopt` first."), ephemeral=True
            )
        if channel is None:
            return await send_card(
                interaction,
                embed=self.embed("🐈 Nowhere to post", "I cannot read that channel."),
                ephemeral=True,
            )
        sent = await send_card(
            channel,
            embed=self.embed("🐈 Dev Cat", devtoy.panel_body(cat, time.time())),
            view=self.panel_view,
        )
        where = getattr(sent, "jump_url", "") or getattr(channel, "mention", "there")
        if interaction.response.is_done():
            await interaction.followup.send(f"Panel posted — {where}", ephemeral=True)
        else:
            await interaction.response.send_message(f"Panel posted — {where}", ephemeral=True)

    async def settings_embed(self, guild_id: int) -> discord.Embed:
        def on(value: bool) -> str:
            return "✅ on" if value else "⛔ off"

        nudges = await self.setting_bool(guild_id, "nudges", default=True)
        chat = await self.setting_bool(guild_id, "reactions", default=True)
        brain = await self.brain_on(guild_id)
        ping = await self.setting_bool(guild_id, "ping", default=False)
        home = await self.setting(guild_id, "home_channel", "") or str(bot_channels.DEV_CHAT_CHANNEL_ID)
        row = await db.devcat_ambient(guild_id)
        state = devtoy.AmbientState(
            last_nudge=float(row.get("last_nudge", 0.0)),
            nudge_day=str(row.get("nudge_day", "")),
            nudge_count=int(row.get("nudge_count", 0)),
        )
        today = devtoy.nudges_today(state, time.time())
        # Which of the two brains is answering is a fact about the *deployment*,
        # and the operator is the only person who can change it — so the card says
        # it rather than leaving "the cat is being clever today" to be read as
        # either "a key is set" or "somebody else's free tier is carrying us".
        source = (
            "this deployment's own key"
            if catfeed.provider_id() == "configured"
            else "a public keyless model"
        )
        body = (
            f"🔔 **Nudges** — {on(nudges)} · the cat's own cards, at most "
            f"{devtoy.NUDGE_DAILY_CAP} a day, {int(devtoy.NUDGE_MIN_GAP // 3600)}h apart\n"
            f"💬 **Chat** — {on(chat)} · answers and reactions to what you type here\n"
            f"🧠 **Brain** — {on(brain)} · sentences it writes itself, by {source}\n"
            f"📣 **Ping** — {on(ping)} · whether those cards may mention you\n"
            f"📌 **Home** — <#{home}>\n\n"
            f"-# {today}/{devtoy.NUDGE_DAILY_CAP} cards today. The panel button posts the "
            "cat's card wherever you point it."
        )
        return self.embed("🐈 Dev Cat settings", body)

    @cat.command(name="settings", description="How loud the cat is allowed to be here.")
    @app_commands.checks.has_permissions(administrator=True)
    async def cat_settings(self, interaction: discord.Interaction):
        embed = await self.settings_embed(interaction.guild_id)
        await send_card(interaction, embed=embed, view=self.settings_view, ephemeral=True)

    # ------------------------------------------------------------------ #
    # /cat bored — the arcade
    # ------------------------------------------------------------------ #
    # Under `/cat` rather than at the root, and Discord's own ceiling is the
    # reason: a tree may hold **one hundred** top-level commands and this one
    # already holds a hundred, so a second root is not a design choice — it is the
    # one that makes `cogs.claims` fail to load at all (`CommandLimitReached`).
    # Nothing is lost: the hub is a card of buttons, so the command is only ever
    # the door to it.
    @cat.command(
        name="bored",
        description="🎮 Nothing to do? The cat has a few ideas, and keeps your records.",
    )
    async def cat_bored(self, interaction: discord.Interaction):
        guild_id, user_id = interaction.guild_id, interaction.user.id
        records = await db.devcat_records(guild_id, user_id)
        lines = []
        for game in devtoy.GAMES:
            held = records.get(game.id) or {}
            best = held.get("best") if held else None
            plays = int(held.get("plays") or 0) if held else 0
            lines.append(
                f"{game.label} — **{devtoy.record_text(game.id, best)}**"
                f" ({game.record})" + (f" · {plays} played" if plays else "")
            )
        cat = await self._load(guild_id, user_id)
        hello = (
            f"{devtoy.face(cat)} **{cat.name or devtoy.DEFAULT_NAME}** is up for anything."
            if cat
            else "You have no cat here yet (`/cat adopt`), but the games work anyway."
        )
        body = f"{hello}\n\n" + "\n".join(lines) + "\n\n-# Press one. Records are yours alone."
        await send_card(interaction, embed=self.embed("🎮 The arcade", body), view=self.hub_view)


async def setup(bot: commands.Bot):
    await bot.add_cog(DevToy(bot))
