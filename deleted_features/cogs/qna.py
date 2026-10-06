"""`/qna` — the anonymous question box.

The shape is three messages for one question:

1. `/qna ask` opens a form. The question goes out through the relay **webhook**
   (``QNA_WEBHOOK_URL``), so the channel sees a question with nobody's name on
   it — not a disguised member, an absent one.
2. The owner gets a **DM about the same question that does name the asker**, with
   an Answer button on it. That asymmetry is the whole feature: the server reads
   the question, one person reads who asked it.
3. The owner's answer is **DMed to the asker** and never posted, because an
   answer posted in the channel answers the wrong person — it tells everybody
   that somebody asked, which is the one fact the asker was promised away.

Where the "who asked" fact lives, and why it is safe there:

* it is written once, in ``qna_questions``, beside the question text;
* it is rendered in exactly two places — the owner's DM and ``/qna inbox`` — and
  neither renders it anywhere else. **Who may read it is the operator's side of
  the box, and that is two people rather than one**: the bot's owner anywhere, and
  somebody holding the **Dev** role *in the bot's own server*, which is the same
  role a release note pings and the same server the inbox lives in. ``/qna
  inbox`` and ``/qna reply`` refuse to run outside that server at all — a
  question is asked *in* a server and answered from the operator's, and a command
  that answers anonymously from the wrong one is a command that looks like it
  works while the answer goes to nobody; and ``/qna ask`` is refused *inside* that
  server, because a question asked where the person answering is already reading
  is not anonymous to anybody;
* the *public* message is not built from that row at all. It is built by
  ``utils/qna.relay_payload``, which reads three fields — the question's id, its
  text and the time — and none of them is a person, and the test that pins it
  builds a payload from a row whose asker is unmistakable and fails if any of it
  appears.

Two facts about Discord that shape how this is written, both verified against the
installed library rather than assumed:

* a **modal's submit is dispatched only to a modal the client already knows
  about**, and ``InteractionResponse.send_modal`` is what registers it (by
  ``custom_id``, in the view store). So the answer form does not need
  ``add_view``, and it cannot be created with a *fixed* ``custom_id``: two answer
  forms open at once would overwrite each other's registration and one of them
  would answer the wrong question. The question's id rides in the ``custom_id``,
  which makes every form unique.
* a **button on an old message is dispatched by ``custom_id`` once the process
  that sent it is gone**, which is why the Answer button carries no state and the
  card's *message id* is what finds the question: one view registered at boot
  answers every card ever sent, however long ago it arrived.
"""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from utils import admin_limits, bot_channels, config_audit, qna, style

#: The one button the owner's card carries. Deliberately does **not** contain the
#: question id: a fixed ``custom_id`` is what lets a single registered view answer
#: a card that was sent weeks ago and a redeploy ago.
REPLY_BUTTON_ID = "qna_reply"

#: What an answer form's ``custom_id`` starts with, followed by the question id.
ANSWER_MODAL_PREFIX = "qna_answer:"

#: How many questions ``/qna inbox`` shows by default. A card holds a lot before
#: Discord refuses it, but an operator reads a list, not a wall.
INBOX_DEFAULT = 5
INBOX_MAX = 10

#: How much of each question the inbox card shows. Enough to recognise it, short
#: enough that a full card of them stays inside what Discord will accept.
INBOX_PREVIEW = 300


# --------------------------------------------------------------------------- #
# Answering an interaction, wherever it came from
# --------------------------------------------------------------------------- #
async def _reply(interaction: discord.Interaction, text: str, *, ephemeral: bool = True):
    """Say one thing, to a guild interaction or a DM one, in any state.

    ``ephemeral`` is dropped for a DM: Discord has no such thing there, and a
    parameter it rejects would turn a sentence into "An unexpected error
    occurred".
    """
    kwargs: dict = {"content": text}
    if ephemeral and getattr(interaction, "guild", None) is not None:
        kwargs["ephemeral"] = True
    if interaction.response.is_done():
        return await interaction.followup.send(**kwargs)
    return await interaction.response.send_message(**kwargs)


async def _is_owner(interaction: discord.Interaction) -> bool:
    """Whether this is the person the owner's surfaces are for.

    Cheapest answer first — the ids this deployment is configured with, which
    costs nothing and covers ``OWNER_USER_ID`` and the compiled defaults — and
    only then Discord's own ``is_owner``, which is an API call the first time.
    Never raises: not being able to ask is not a yes.
    """
    user = getattr(interaction, "user", None)
    if user is None:
        return False
    if admin_limits.is_owner_user(getattr(user, "id", None)):
        return True
    check = getattr(getattr(interaction, "client", None), "is_owner", None)
    if not callable(check):
        return False
    try:
        return bool(await check(user))
    except Exception:
        return False


#: What somebody is told when they reach the operator's side of the box from the
#: wrong server. The distinction the message has to make is between *wrong place*
#: and *wrong person*, because they are fixed by different things.
OPERATOR_SERVER_ONLY = (
    "❌ This is the operator's side of the anonymous box, and it only runs in the "
    "bot's own servers — the ones its owner answers from. `/qna ask` works in "
    "every other server, including this one's members."
)

#: And what they are told from the right server with the wrong roles.
OPERATOR_ONLY = (
    "❌ Only the bot's owner, or somebody with the **Dev** role in this server, "
    "answers anonymous questions."
)


def _in_home_server(interaction: discord.Interaction) -> bool:
    """Whether this happened in a server the bot treats as its own.

    One reader of ``bot_channels.is_own_server``, so the three answers this file
    gives — where the inbox lives, who may open it and who may answer — can never
    disagree about where that is. It is **two servers**, which is the part that is
    easy to get wrong and was: this bot is in the one it now reports from *and* the
    one its release notes were hardcoded to first, the Dev role the ping has always
    tagged was created in the second, and a gate naming only the first refused in
    the server the operator was sitting in.

    A DM is not either of them: an interaction without a guild is the owner's card,
    which the owner answers from anywhere.
    """
    return bot_channels.is_own_server(getattr(interaction, "guild", None))


async def _is_operator(interaction: discord.Interaction) -> bool:
    """Whether this person may read a question's asker and answer for them.

    Two ways in and no third: the **owner**, whose DM the question arrives in, and
    a member holding the **Dev** role *in the bot's own server*. Read through
    ``bot_channels`` rather than by name here, so a server that renamed its role —
    or the deployment that has the role's id — keeps working, and so the ping a
    release note carries and the gate on this command cannot drift apart.
    """
    if not _in_home_server(interaction):
        return False
    if await _is_owner(interaction):
        return True
    guild = getattr(interaction, "guild", None)
    role = bot_channels.dev_role(guild)
    member = getattr(interaction, "user", None)
    return role is not None and role in (getattr(member, "roles", None) or [])


def _owner_id() -> int:
    """The configured owner's user id, at call time rather than at import.

    Read here rather than from ``utils.admin_limits``' module constant because
    that one is fixed when the process starts and this feature's only use for it
    is *where to send a DM* — which is worth no DM at all when nobody is
    configured. The compiled fallback ids cannot help: they say "these are
    operators", not "this is the one to write to".
    """
    raw = config_audit.get("OWNER_USER_ID", "0") or "0"
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _name_of(thing) -> str:
    """Somebody's readable name, for the owner's eyes only."""
    for attribute in ("display_name", "global_name", "name"):
        value = getattr(thing, attribute, None)
        if value:
            return str(value)
    return str(getattr(thing, "id", "") or "unknown")


# --------------------------------------------------------------------------- #
# The two things a question produces
# --------------------------------------------------------------------------- #
async def notify_owner(bot, record: dict) -> bool:
    """DM the owner the question *and* who asked it. Returns whether it landed.

    The DM is a courtesy, not the record: the question is already in
    ``qna_questions``, so a closed DM means the owner finds it in ``/qna inbox``
    instead. That is why a failure here is logged and swallowed rather than
    allowed to touch the asker, who has already been told their question went out.
    """
    owner_id = _owner_id()
    if not owner_id:
        print(
            f"[QNA] Question {record.get('id')} asked, and nobody was told who asked "
            "it: OWNER_USER_ID is not set, so there is no DM to send. It is still "
            "readable with /qna inbox.",
            flush=True,
        )
        return False
    try:
        owner = bot.get_user(owner_id) or await bot.fetch_user(owner_id)
    except Exception as exc:
        print(
            f"[QNA] Could not look up the owner ({type(exc).__name__}) to tell them "
            f"about question {record.get('id')}.",
            flush=True,
        )
        return False

    card = style.Panel(
        title="❓ An anonymous question",
        body=qna.question_text(record),
        accent=style.ACCENT,
        fields=qna.owner_fields(record),
        footer="Only you can see who asked. The answer is DMed, never posted.",
        timeout=None,
    )
    card.add_row(reply_button())
    try:
        message = await owner.send(view=card, allowed_mentions=discord.AllowedMentions.none())
    except Exception as exc:
        print(
            f"[QNA] Could not DM the owner about question {record.get('id')} "
            f"({type(exc).__name__}); /qna inbox has it.",
            flush=True,
        )
        return False
    await qna.attach_owner_message(record.get("id"), getattr(message, "id", 0))
    return True


def reply_button() -> discord.ui.Button:
    """The Answer button, in the two places it has to exist.

    One definition, two uses: it rides on the owner's card (where the message it
    sits on is what dispatch matches it by, for as long as this process lives)
    and it is the single item of :class:`QnaReplyView` (registered once at boot,
    which is what makes a card clicked the next morning still answer).
    """
    button = discord.ui.Button(
        label="Answer",
        style=discord.ButtonStyle.primary,
        emoji="✉️",
        custom_id=REPLY_BUTTON_ID,
    )
    button.callback = open_reply_modal
    return button


class QnaReplyView(discord.ui.View):
    """What the Answer button keeps doing after a redeploy.

    A card is found by its *message id* while the process that sent it is alive,
    and by ``custom_id`` forever otherwise — and only a registered view exists in
    that second sense. Registering it once, with no state in it, is what lets the
    question be looked up from the interaction instead of carried in the button.
    """

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(reply_button())


# --------------------------------------------------------------------------- #
# Asking
# --------------------------------------------------------------------------- #
class AskModal(discord.ui.Modal, title="Ask anonymously"):
    """The question form. Opened by ``/qna ask``, and never posted anywhere."""

    question = discord.ui.TextInput(
        label="Your question",
        placeholder="Anything — it is posted with nobody's name on it",
        style=discord.TextStyle.paragraph,
        max_length=qna.MAX_QUESTION,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await submit_question(interaction, str(self.question.value or ""))


async def submit_question(interaction: discord.Interaction, text: str) -> None:
    """Relay one question, then tell the two people it concerns."""
    guild = getattr(interaction, "guild", None)
    if guild is None:
        return await _reply(
            interaction,
            "❓ Use `/qna` in a server — a question is posted to that server's "
            "anonymous box, and the answer comes back here.",
        )
    question = (text or "").strip()
    if not question:
        return await _reply(interaction, "❓ The box was empty, so nothing was posted.")
    if not qna.configured():
        # Named, not guessed at: "it does nothing" and "it is switched off" look
        # identical from in here, and this is the version somebody can act on.
        return await _reply(
            interaction,
            "⚠️ The anonymous box is not connected yet — the bot's owner has to set "
            f"`{qna.SETTING}` to the webhook of a channel for it. Nothing of yours "
            "was posted.",
        )

    # Written down *before* the relay is attempted, so a webhook that refuses the
    # post leaves a row saying so instead of losing the question.
    record = await qna.record(
        guild_id=getattr(guild, "id", 0),
        guild_name=getattr(guild, "name", ""),
        user_id=interaction.user.id,
        user_name=_name_of(interaction.user),
        question=question,
    )
    # Deferred before the request goes out: the webhook has its own eight-second
    # timeout and an interaction token does not wait that long.
    await interaction.response.defer(ephemeral=True)
    ok, detail = await asyncio.to_thread(
        qna.post, qna.webhook_url(), qna.relay_payload(record)
    )
    await qna.note_relay(record.get("id"), ok, detail)
    if not ok:
        print(
            f"[QNA] Question {record.get('id')} was not relayed: {detail}",
            flush=True,
        )
        return await interaction.followup.send(
            "❌ The box refused it, so nothing appeared in the channel. It is not "
            "lost — the bot's owner can see it in `/qna inbox`.",
            ephemeral=True,
        )
    await interaction.followup.send(
        "✅ Asked anonymously. If there's an answer, I'll DM it to you here.",
        ephemeral=True,
    )
    await notify_owner(interaction.client, record)


# --------------------------------------------------------------------------- #
# Answering
# --------------------------------------------------------------------------- #
class ReplyModal(discord.ui.Modal):
    """The answer form, opened by the card's button or by ``/qna reply``.

    The question id is in the ``custom_id`` rather than taken from the card,
    because two forms can be open at once and a fixed id would make the second
    overwrite the first's registration — one of them would then answer the wrong
    question. ``send_modal`` registers this by itself (that is how a modal's
    submit is dispatched at all), so nothing here calls ``add_view``.
    """

    answer = discord.ui.TextInput(
        label="Your answer",
        placeholder="DMed to whoever asked — it is never posted",
        style=discord.TextStyle.paragraph,
        max_length=qna.MAX_ANSWER,
        required=True,
    )

    def __init__(self, question_id: int, *, handle: str = ""):
        title = f"Answer {handle or 'the question'}"
        super().__init__(
            title=title[:45],
            custom_id=f"{ANSWER_MODAL_PREFIX}{int(question_id)}",
        )
        self.question_id = int(question_id)

    async def on_submit(self, interaction: discord.Interaction):
        await deliver_reply(interaction, self.question_id, str(self.answer.value or ""))


async def open_reply_modal(interaction: discord.Interaction) -> None:
    """Open the answer form for the question this card is about.

    The card is a DM — it is DMed to the owner and to nobody else — so this is the
    owner's own button and the rule here is the owner's alone. ``/qna reply``, the
    other way to this form, is where a Dev is let in, and that command checks the
    server and the role before it ever gets here.
    """
    if not await _is_owner(interaction):
        return await _reply(
            interaction, "❌ Only the bot's owner answers anonymous questions."
        )
    card_message = getattr(interaction, "message", None)
    record = await qna.for_owner_message(getattr(card_message, "id", 0))
    if record is None:
        return await _reply(
            interaction,
            "❌ I can't tell which question this card is about any more — "
            "`/qna inbox` still has it.",
        )
    if record.get("status") == qna.ANSWERED:
        return await _reply(
            interaction, f"✅ {qna.handle(record)} has already been answered."
        )
    await interaction.response.send_modal(
        ReplyModal(record.get("id"), handle=qna.handle(record))
    )


def _outcome(
    outcome: str,
    *,
    ok: bool = False,
    question_id: int = 0,
    handle: str = "",
    asker: str = "",
    reason: str = "",
) -> dict:
    """One answer's result, in one shape — so a surface never reads a bare ``None``.

    ``id`` travels with every outcome because the "I have no such question"
    sentence names it: an operator who typed or picked an id is owed the number
    back, not a remark about there being no question *like* it.
    """
    return {
        "ok": ok,
        "outcome": outcome,
        "id": int(question_id or 0),
        "handle": handle,
        "asker": asker,
        "reason": reason,
    }


async def deliver_answer(client, question_id: int, answer: str) -> dict:
    """DM the asker and close the question, or say why neither happened.

    Every surface that can answer goes through this function — the button on the
    owner's card, `/qna reply`, and the console's own Questions page. That is not
    tidiness: the *ordering* below is the entire protection, and a second
    implementation for the web would be a second place for it to be wrong.

    The question is **claimed before the DM is sent** — one conditional update, so
    two clicks on an old card, or a reply from the console and one from a phone,
    cannot DM somebody twice — and **put back in the inbox** if the DM itself
    fails, which is the one case where claiming first would otherwise cost the
    answer. Nothing is thrown: a refused DM is an outcome to report, not a 500.

    Returns ``{ok, outcome, id, handle, asker, reason}``, which
    :func:`answer_sentence` turns into words — the same words on every surface,
    because an answer that did not arrive is exactly the thing an operator would
    assume worked.
    """
    text = (answer or "").strip()
    if not text:
        return _outcome("empty", question_id=question_id)

    record = await qna.get(question_id)
    if record is None:
        return _outcome("unknown", question_id=question_id)

    handle = qna.handle(record)
    asker = str(record.get("user_name") or "they")
    if not await qna.mark_answered(question_id, text):
        return _outcome("already", question_id=question_id, handle=handle, asker=asker)

    user = None
    reason = "I couldn't look their account up"
    asker_id = record.get("user_id")
    try:
        user = client.get_user(asker_id) or await client.fetch_user(asker_id)
    except Exception as exc:
        reason = f"Discord would not give me their account ({type(exc).__name__})"
        print(
            f"[QNA] Could not resolve the asker of question {question_id}: {exc}",
            flush=True,
        )

    if user is not None:
        card = style.Panel(
            title="📨 An answer to your question",
            body=qna.answer_body(record, text),
            accent=style.SUCCESS,
            footer="You asked anonymously — this came to you alone.",
            timeout=None,
        )
        try:
            await user.send(view=card, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            reason = f"Discord refused the DM ({type(exc).__name__})"
            print(
                f"[QNA] Could not DM the asker of question {question_id}: {exc}",
                flush=True,
            )
            user = None

    if user is None:
        # Given back, not lost: the answer is not DMed, so it has not been given.
        await qna.reopen(question_id)
        return _outcome(
            "refused", question_id=question_id, handle=handle, asker=asker, reason=reason
        )
    return _outcome("sent", ok=True, question_id=question_id, handle=handle, asker=asker)


def answer_sentence(result: dict, *, inbox: str = "/qna inbox", bold: bool = True) -> str:
    """One outcome, in words — the same words wherever the answer was typed.

    Two things differ between the surfaces, and both are about *rendering* rather
    than about what happened: on Discord the place a question goes back to is a
    command and the asker's name is emphasised with markdown, and in the console
    it is the list the reader is looking at and the name is plain text. Found by
    reading the toast in a browser: `**Meow**` is the right sentence on a card and
    four stray asterisks in HTML, where nothing renders them.
    """
    outcome = str((result or {}).get("outcome") or "")
    handle = (result or {}).get("handle") or "the question"
    if outcome == "sent":
        asker = str((result or {}).get("asker") or "them")
        return (
            f"✅ Sent to {f'**{asker}**' if bold else asker} as a DM, and {handle} "
            "is closed. It was not posted anywhere."
        )
    if outcome == "already":
        return f"✅ {handle} already has an answer — nothing was sent twice."
    if outcome == "empty":
        return "❌ An empty answer is not an answer — nothing was sent."
    if outcome == "unknown":
        return f"❌ I have no question with the id `{(result or {}).get('id')}`."
    return (
        f"❌ Not delivered — {(result or {}).get('reason')}. It is back in {inbox} "
        "and your answer is still here, so nothing was lost."
    )


async def deliver_reply(
    interaction: discord.Interaction, question_id: int, answer: str
) -> None:
    """The same delivery, from a slash command or the button on the owner's card.

    Only two things are this function's own: the check on who is answering (Discord
    is the only place a caller could be somebody else) and the wording's ``inbox``.
    Two shapes of caller reach here, which is why the check is not simply the
    owner: the modal opened from ``/qna reply`` in the bot's own server, where a
    **Dev** answers too, and the modal opened from the owner's own card in a DM,
    where there are no roles to read and the owner is the only person who has it.
    """
    if getattr(interaction, "guild", None) is None:
        if not await _is_owner(interaction):
            return await _reply(
                interaction, "❌ Only the bot's owner answers anonymous questions."
            )
    elif not await _is_operator(interaction):
        return await _reply(interaction, OPERATOR_ONLY)
    result = await deliver_answer(interaction.client, question_id, answer)
    return await _reply(interaction, answer_sentence(result))


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #
class Qna(commands.Cog):
    """Anonymous questions, relayed by a webhook and answered by DM."""

    qna_group = app_commands.Group(
        name="qna",
        description="Ask something anonymously, and answer what other people asked.",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        #: Registered now rather than on the first DM: this is what makes the
        #: Answer button on a card sent yesterday still work today, and a button
        #: that only works while the process that sent it is alive is a button
        #: that stops working at the next deploy.
        bot.add_view(QnaReplyView())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and hasattr(self.bot, "is_module_enabled"):
            # Gated by **legacy**, not by "qna": the question box is the other
            # half of the one switch an administrator flips. The inbox and the
            # reply live in the bot's own server and their audience is unchanged.
            if not self.bot.is_module_enabled(interaction.guild_id, "legacy"):
                await interaction.response.send_message(
                    "⚠️ **Anonymous Q&A is part of the legacy module**, which is "
                    "currently **turned off** on this server.\nAn administrator can "
                    "turn it on with "
                    "`/bot legacy state:On`, or in the "
                    "[Web Dashboard](https://dacatdashboard.pages.dev/dashboard).",
                    ephemeral=True,
                )
                return False
        return True

    @qna_group.command(
        name="ask",
        description="Ask a question with nobody's name on it.",
    )
    async def qna_ask(self, interaction: discord.Interaction):
        """Ask here; answered from the bot's own server by its owner or a Dev.

        Refused in that server on purpose: the relay posts the question into the
        channel it happens in, and the channel the person answering reads is the
        one place the name being hidden is the name of the only reader.
        """
        if getattr(interaction, "guild", None) is None:
            return await _reply(
                interaction,
                "❓ Use `/qna` in a server — a question is posted to that server's "
                "anonymous box, and the answer comes back here.",
            )
        if _in_home_server(interaction):
            # The bot's own servers are where these are *answered*, and a question
            # asked there is a question with nowhere to go: the relay posts it into
            # that server, which is the one channel the person answering is already
            # reading. Somebody with something to say to the operator writes to the
            # operator; the box is for the servers the bot is a visitor in.
            return await _reply(
                interaction,
                "❌ `/qna ask` is for the servers the bot is in — it is anonymous "
                "because the *server* reads the question and one person reads who "
                "asked it, and in the bot's own servers those are the same person. "
                "Use it somewhere else, or write to the operator directly.",
            )
        if not qna.configured():
            return await _reply(
                interaction,
                "⚠️ The anonymous box is not connected yet — the bot's owner has to set "
                f"`{qna.SETTING}` to the webhook of a channel for it.",
            )
        await interaction.response.send_modal(AskModal())

    @qna_group.command(
        name="inbox",
        description="The questions still waiting for an answer (bot's own server only).",
    )
    @app_commands.describe(limit="How many to show.")
    async def qna_inbox(self, interaction: discord.Interaction, limit: int = INBOX_DEFAULT):
        """The waiting questions and who asked them — the owner, or a Dev here."""
        if not _in_home_server(interaction):
            return await _reply(interaction, OPERATOR_SERVER_ONLY)
        if not await _is_operator(interaction):
            return await _reply(interaction, OPERATOR_ONLY)
        wanted = max(1, min(int(limit or INBOX_DEFAULT), INBOX_MAX))
        rows = await qna.open_questions(limit=wanted)
        if not rows:
            return await _reply(interaction, "📭 No questions are waiting.")
        totals = await qna.counts()
        card = style.Panel(
            title="📬 Anonymous questions waiting",
            body=(
                f"**{totals.get('open', len(rows))}** waiting, newest first. Answer one "
                "with the button on your DM, or `/qna reply question:<id>` — the answer "
                "is DMed to whoever asked and never posted.\n"
                "-# Only you see these names; the channel saw the question on its own."
            ),
            accent=style.ACCENT,
            fields=[
                (
                    f"{qna.handle(record)} · #{record.get('id')}",
                    f"**{record.get('user_name')}** in {record.get('guild_name')}\n"
                    # Trimmed: ten full questions would run past what one card may
                    # carry, and a card Discord refuses answers nothing at all. The
                    # whole question is on the DM the owner already has.
                    f"{qna.question_text(record)[:INBOX_PREVIEW]}",
                )
                for record in rows
            ],
            footer=f"Answered so far: {totals.get('answered', 0)}",
            timeout=None,
        )
        await style.send_panel(interaction, card, ephemeral=True)

    @qna_group.command(
        name="reply",
        description="Answer a question — the answer is DMed to them (bot's own server).",
    )
    @app_commands.describe(question="The question's id, from /qna inbox. Defaults to the newest.")
    async def qna_reply(
        self, interaction: discord.Interaction, question: int | None = None
    ):
        """Open the answer form for a waiting question. Owner or Dev, here only."""
        if not _in_home_server(interaction):
            return await _reply(interaction, OPERATOR_SERVER_ONLY)
        if not await _is_operator(interaction):
            return await _reply(interaction, OPERATOR_ONLY)
        if question is None:
            waiting = await qna.open_questions(limit=1)
            if not waiting:
                return await _reply(interaction, "📭 There is nothing waiting to answer.")
            record = waiting[0]
        else:
            record = await qna.get(question)
            if record is None:
                return await _reply(interaction, f"❌ I have no question with the id `{question}`.")
            if record.get("status") == qna.ANSWERED:
                return await _reply(
                    interaction,
                    f"✅ {qna.handle(record)} has already been answered — "
                    "`/qna inbox` lists the ones still waiting.",
                )
        await interaction.response.send_modal(
            ReplyModal(record.get("id"), handle=qna.handle(record))
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Qna(bot))
