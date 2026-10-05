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


def title_for(lane: str) -> str:
    return TITLE_BY_LANE.get(str(lane), TITLE_BY_LANE["factual"])
