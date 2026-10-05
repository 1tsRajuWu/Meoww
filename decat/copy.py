"""Every sentence ``/decat`` says, written down once and pinned by tests.

The copy lives in its own module for two reasons, and only the second is about
taste.

The first: the tone here is the operator's, not the framework's. The line for a
laptop that is simply not awake yet is *"sry, offline rn"*, and it is that way
because that is what was asked for. A refactor that improves the wording without
being asked to is a refactor that changed the product, so the words are constants
with tests holding them.

The second: the two lanes that must never be lost in a rewrite are the AI
disclosure and the not-a-professional notice. Those are not decoration. An answer
to "is this contract enforceable" that reads like advice from a lawyer is a
worse outcome than no answer at all, so the rule that attaches the notice is a
**deterministic function of the question** — :func:`lane_of` and
:func:`warning_for` — and does not depend on the model remembering to mention it.
The model gets told to write it too; this is the copy that appears whether or not
it does.
"""
from __future__ import annotations

from decat import protocol

# ── the three ways it is not there ───────────────────────────────────────────

#: The laptop is not polling: asleep, shut, off the network, or the agent is not
#: running. One line for all four, because from Discord they are one thing.
OFFLINE = "sry, offline rn — can't really msg bbg 😿"

#: The laptop is awake and answering, and the switch on it says no.
SWITCHED_OFF = "the Mac's switched off rn — flip the switch on it and ask me again 🙂"

#: It took the question and never came back.
WENT_QUIET = "the Mac went quiet mid-question — try me once more?"

#: Somebody else's question is in flight.
BUSY = "the Mac's chewing on somebody else's question rn — give it a few secs 🐈"

#: Nothing is configured to answer yet (no token, or the deployment names no
#: server and this one's switch is off).
NOT_CONFIGURED = (
    "decat isn't wired up here yet — the operator has to point it at a machine "
    "first. `/bot decat` says what is missing."
)

#: This server is not one the deployment names.
NOT_YOUR_SERVER = "decat doesn't run in this server."

#: The system is off for this server. "Turned off" rather than "switched off"
#: on purpose: this is the module gate's sentence, and the suite's law about a
#: module refusal (`tests/test_command_scope.py`) reads that phrase to check the
#: refusal is the *module* gate rather than some other wall in front of the
#: command. The Mac's own switch has its own line (`SWITCHED_OFF`), because from
#: Discord they are different questions with different fixes.
SYSTEM_OFF = "Decat is turned off on this server — an administrator can switch it on."

TOO_LONG = "that question is {length} characters — {limit} is the most I can carry."
COOLDOWN = "one at a time, bbg — {seconds}s."

# ── the two notices that are not optional ───────────────────────────────────

#: The small warning asked for. It is attached to anything legal, medical,
#: financial, safety-related or personal — never to "what is the capital of Peru".
PROFESSIONAL_WARNING = (
    "⚠️ I'm an AI, not a professional — for legal, medical, money or safety "
    "decisions, talk to somebody qualified."
)

#: For the questions that read like somebody in danger. Deliberately short, and
#: deliberately not a phone number: a helpline that is wrong for the reader's
#: country is worse than no number at all, and the finder covers every country.
CRISIS = (
    "🫂 If you're in danger or thinking about hurting yourself, please talk to "
    "someone now — an emergency service where you live, or a crisis line from "
    "https://findahelpline.com. I'm a bot on a laptop, and I can't keep you safe."
)

#: Every answer says what made it. A reader is entitled to know it was a machine.
DISCLOSURE = "answered by {model} on the operator's own Mac — not a person."

# ── card chrome ─────────────────────────────────────────────────────────────

TITLE_BY_LANE = {
    "factual": "🔎 Decat",
    "advice": "🧭 Decat",
    "personal": "🫂 Decat",
}
SOURCES_TITLE = "📎 Sources"
GROUNDED_LABEL = "checked against the sources it cites"
#: The `Fast` depth earns its own sentence, and it is not a nicety. A fast answer
#: is grounded in sources it did not go back over, so calling it *checked* would be
#: the same lie as dressing an ungrounded one up as cited — the two claims a reader
#: is entitled to trust are "this was looked up" and "this was re-read", and the
#: second one is exactly what Fast skips.
QUICK_LABEL = "drafted from the sources it cites — the quick answer, not re-read"
UNGROUNDED_LABEL = "⚠️ no live sources reached — this is the model's own knowledge"
NO_QUESTION_NOTE = "asked privately by {user}"

# ── the rule ────────────────────────────────────────────────────────────────

#: Words that put a question in the *personal* lane, where the answer listens
#: first and prescribes nothing.
PERSONAL_WORDS: frozenset[str] = frozenset({
    "depressed", "depression", "anxious", "anxiety", "panic", "panicking",
    "lonely", "loneliness", "burnout", "burnt out", "grief", "grieving",
    "heartbroken", "breakup", "broke up", "divorce", "suicidal", "suicide",
    "self harm", "self-harm", "cutting", "eating disorder", "anorexi", "bulimi",
    "therapist", "therapy", "counselling", "counseling", "mental health",
    "hate myself", "can't cope", "cant cope", "no point anymore", "worthless",
    "abuse", "abusive", "trauma", "scared of him", "scared of her",
})

#: Words that put a question in the *advice* lane: real decisions with real
#: consequences, where a wrong confident sentence costs somebody something.
ADVICE_WORDS: frozenset[str] = frozenset({
    "lawyer", "legal", "illegal", "sue", "lawsuit", "contract", "clause",
    "tenant", "landlord", "evict", "visa", "immigration", "deport", "custody",
    "court", "plea", "arrest", "police", "criminal", "fine", "ticket",
    "tax", "taxes", "hmrc", "irs", "invoice", "vat", "gst",
    "invest", "investment", "stocks", "crypto", "mortgage", "loan", "debt",
    "pension", "insurance", "salary", "raise", "redundancy", "laid off",
    "medication", "medicine", "dose", "dosage", "antibiotic", "prescription",
    "symptom", "diagnos", "surgery", "side effect", "blood test", "screening",
    "pregnant", "contraception", "emergency contraception", "std", "sti",
    "allerg", "asthma", "insulin", "cancer", "tumour", "tumor",
    "emergency", "overdose", "poison", "breathe", "breathing trouble",
    # Safety questions that name no illness and no lawyer, and would otherwise be
    # answered as trivia: "is it safe to drive with a broken wrist" is a question
    # about somebody's body, and it deserves the same notice as "what dose".
    "is it safe", "safe to take", "safe to drive", "safe to mix", "safe to eat",
    "broken bone", "broken wrist", "broken arm", "fracture", "sprain", "concussi",
    "chest pain", "can't breathe", "cant breathe", "bleeding", "burned", "burnt my",
})

CRISIS_WORDS: frozenset[str] = frozenset({
    "suicidal", "suicide", "kill myself", "killing myself", "end my life",
    "self harm", "self-harm", "hurt myself", "overdose", "jump off",
    "no reason to live", "want to die", "wish i was dead", "better off dead",
})


def _hit(text: str, words: frozenset[str]) -> bool:
    return any(word in text for word in words)


def lane_of(question: str) -> str:
    """Which of the three ways this question should be answered.

    *factual* — find it and cite it.
    *advice* — real decisions with real consequences: options and trade-offs,
    labelled as opinion, never an instruction.
    *personal* — somebody's own life: listen first, diagnose nothing.
    """
    text = f" {str(question or '').lower()} "
    if _hit(text, PERSONAL_WORDS):
        return "personal"
    if _hit(text, ADVICE_WORDS):
        return "advice"
    return "factual"


def crisis_wording(question: str) -> bool:
    """Whether this question reads like somebody who needs a person, now."""
    return _hit(f" {str(question or '').lower()} ", CRISIS_WORDS)


def warning_for(question: str) -> str:
    """The notice this question earns, or ``""`` when it earns none.

    Kept a function of the *question* rather than of the answer for the honest
    reason: the answer is the part that can be wrong, and a sentence about who is
    qualified to answer must not depend on whether the model felt like it.
    """
    if crisis_wording(question):
        return CRISIS
    if lane_of(question) in {"advice", "personal"}:
        return PROFESSIONAL_WARNING
    return ""


# ── while it is thinking ────────────────────────────────────────────────────

#: The card that appears the moment a question is accepted. Discord's own deferred
#: placeholder says *"Bot is thinking…"* in Discord's voice, and the wait is the
#: one part of this feature with nothing to look at — so the wait gets a card of
#: its own, in the operator's voice, and it is replaced by the answer in place.
THINKING_TITLE = "🛰️ Decat is thinking…"

#: Picked per question, by a hash of the job id: varied between questions, stable
#: for one, which is what makes it testable and what stops the same line greeting
#: somebody twice in a row. Written to be true as well as funny — a joke that lies
#: about what the machine is doing would be worse here than no joke at all.
THINKING_LINES = (
    "one goldfish brain, several web pages. give it a second.",
    "reading the internet so you don't have to — and it will cite what it read.",
    "asking the web, then asking itself whether the web agreed.",
    "the cat is on the laptop. the laptop is doing the thinking.",
    "no model vendor is being called for this. one Mac, slowly and honestly.",
    "finding the sources first and the opinion second.",
    "if a fan spins up somewhere, that's the answer being written.",
    "cntrl+f'ing the entire internet. roughly.",
)

#: What a question about somebody's own life gets instead, and the rule the rest of
#: this section turns on: **a joke is a gift you can only give to somebody who is
#: not upset.** A person who has just typed that they cannot cope is not waiting to
#: be amused, and the waiting card is the first thing they see rather than the last.
THINKING_KIND = "give it a moment — this one gets thought about properly."

#: Longest `/quote` line this will show while it thinks: a line you have to finish
#: reading is not a line about waiting.
SHORT_QUOTE_MAX = 88


def jokes_allowed(question: str) -> bool:
    """Whether this question may be met with a joke.

    False for the *personal* lane and for anything that reads like somebody in
    danger — not because those questions are not answered, but because the answer
    is not the first thing they see (see :data:`THINKING_KIND`).
    """
    return lane_of(question) != "personal" and not crisis_wording(question)


def pick_index(count: int, seed: str) -> int:
    """A stable index into ``count`` for ``seed`` — a hash, not a coin flip.

    ``random`` would make every assertion in the tests either flaky or pinned to
    one value; a hash of the job id is varied across questions and reproducible
    for one, which is the property that matters in both places.
    """
    if count <= 0:
        return 0
    digest = 0
    for char in str(seed or ""):
        digest = (digest * 31 + ord(char)) % 1_000_003
    return digest % count


def _quote_text(row) -> str:
    """The line out of one ``/quote`` entry, or ``""`` for anything else."""
    return str(row.get("quote") or "").strip() if isinstance(row, dict) else ""


def select_quote(rows, seed: str = "") -> tuple[str, str]:
    """One short line out of a quote corpus, with who said it — or ``("", "")``.

    The corpus itself is passed in rather than imported, so this module keeps
    holding *rules* and somebody else's four-hundred-line quote list stays in the
    one cog that already maintains it.

    A long line is dropped rather than cut short, and that is the whole rule
    worth writing down: a quotation trimmed to fit is a misquotation, so a
    corpus with no short line in it yields nothing at all rather than a sentence
    the person who said it never said.
    """
    usable = [
        row
        for row in (rows or ())
        if 0 < len(_quote_text(row)) <= SHORT_QUOTE_MAX
    ]
    if not usable:
        return "", ""
    row = usable[pick_index(len(usable), seed)]
    who = " · ".join(
        str(part) for part in (row.get("character"), row.get("source")) if part
    )
    return _quote_text(row), who


def thinking_line(
    question: str,
    *,
    depth: str = protocol.DEPTH_DEEP,
    seed: str = "",
    quote: str = "",
    who: str = "",
) -> str:
    """What the waiting card says, given what is being asked and how hard.

    ``quote``/``who`` are a line from the bot's own ``/quote`` corpus and its
    attribution. They arrive as arguments rather than being looked up here so that
    the *rules* about them — how long, and who may be shown one — live in this
    module with the rest of the copy, while the corpus stays in the one cog that
    already keeps it.
    """
    funny = jokes_allowed(question)
    body = [f"🧠 {pick(THINKING_LINES, seed)}" if funny else f"🫂 {THINKING_KIND}"]
    clean = str(quote or "").strip()
    if funny and clean and len(clean) <= SHORT_QUOTE_MAX:
        credit = f" — {who}" if str(who or "").strip() else ""
        body.insert(0, f"*“{clean}”*{credit}")
    return "\n\n".join(body)


def thinking_foot(depth: str = protocol.DEPTH_DEEP) -> str:
    """What the machine is doing while the card is on screen — told, not implied.

    The two depths buy different waits, so they get different sentences: a *Fast*
    question that sat there for a minute would otherwise look like a broken Fast.
    """
    if depth == protocol.DEPTH_FAST:
        return "one quick pass on the operator's own Mac — no re-check"
    return "reading the live web on the operator's own Mac — then re-reading its answer"


def pick(lines, seed: str) -> str:
    """One line out of ``lines`` for ``seed``."""
    return lines[pick_index(len(lines), seed)] if lines else ""


def title_for(lane: str) -> str:
    return TITLE_BY_LANE.get(str(lane), TITLE_BY_LANE["factual"])
