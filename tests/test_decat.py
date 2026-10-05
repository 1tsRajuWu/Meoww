"""Decat, from the bot's side: the queue, the words, the gates and the floor.

Everything here runs with no network, no laptop and no model, because the half
that needs those is not this half. What is checked is what the *bot* promises:

* the wire format both machines speak is one format, and a payload this version
  cannot read is refused rather than half-understood;
* the relay keeps nothing — not after an answer, not in its status, not when a
  question expires, and not when somebody is told the machine is offline;
* the three ways it is not there say three different things, and the words are
  the operator's;
* the notice that this is an AI and not a professional is attached by the
  *question*, so it cannot be absent because a model forgot;
* the gates are two gates: the server's own switch and the deployment's
  allow-list, and neither of them can be widened by typing something;
* and the wait itself is a card rather than Discord's placeholder — a joke line or
  a short one out of the bot's own ``/quote`` corpus — except on somebody's own
  life, where the joke is the one thing that must not appear;
* and no question's words can reach the deploy's log ring, which is readable by
  anybody holding the admin passkey.
"""
from __future__ import annotations

import inspect
import os
import time
import unittest

from decat import config, copy, protocol
from decat import relay as relay_module


class _Clock:
    """A clock that only moves when a test moves it."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def cog_source() -> str:
    """The cog's own text, read rather than imported.

    Two claims in this file are about the *source* — that it never prints, and
    that the job is settled before the card is drawn — and reading it means those
    checks run on a machine that cannot import the Discord library at all. It is
    also the stronger form: a helper function printing a question would not be
    caught by inspecting the callback alone.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cogs", "decat.py")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _job(question: str = "what is the capital of Peru", **overrides) -> protocol.Job:
    values = dict(
        id="job1",
        question=question,
        mode=protocol.MODE_PRIVATE,
        guild_id=1550227368889684058,
        channel_id=5,
        user_id=77,
        user_name="somebody",
        created_at=1000.0,
        deadline=1180.0,
    )
    values.update(overrides)
    return protocol.Job(**values)


class TestTheWireFormat(unittest.TestCase):
    def test_a_job_survives_the_round_trip(self):
        job = _job("why is the sky blue", mode=protocol.MODE_PUBLIC, image_url="https://x/y.png")
        back = protocol.Job.from_wire(job.to_wire())
        self.assertEqual(back, job)
        self.assertTrue(back.saves)

    def test_an_answer_survives_the_round_trip(self):
        answer = protocol.Answer(
            id="job1", text="because of scattering", sources=(("Nasa", "https://n.test/a"),),
            model="qwen3-vl:8b", ms=4200, lane="factual", verified=True,
        )
        back = protocol.Answer.from_wire(answer.to_wire())
        self.assertEqual(back.text, answer.text)
        self.assertEqual(back.sources, answer.sources)
        self.assertTrue(back.verified)

    def test_the_depth_survives_the_round_trip(self):
        for depth in protocol.DEPTHS:
            with self.subTest(depth=depth):
                job = _job(depth=depth)
                self.assertEqual(protocol.Job.from_wire(job.to_wire()).depth, depth)
        self.assertEqual(_job().depth, protocol.DEPTH_DEEP, "deep is the default")

    def test_an_unreadable_depth_fails_open_where_an_unreadable_mode_fails_closed(self):
        """The asymmetry is the decision, so it is pinned as one.

        An unreadable mode becomes *private* because saving is what cannot be taken
        back; an unreadable depth becomes *deep* because the cost of the guess is a
        few seconds and the better answer. A version that quietly failed the same
        way in both directions would be passing this test for the wrong reason.
        """
        self.assertEqual(protocol.parse_depth("nonsense"), protocol.DEPTH_DEEP)
        self.assertEqual(protocol.parse_depth(None), protocol.DEPTH_DEEP)
        self.assertEqual(protocol.parse_mode("nonsense"), protocol.MODE_PRIVATE)
        # Case is folded, like the mode's, because Discord hands a choice over as
        # typed; an *unknown* word is the case that falls back.
        self.assertEqual(
            protocol.Job.from_wire({"id": "x", "question": "q", "depth": "FAST"}).depth,
            protocol.DEPTH_FAST,
        )
        self.assertTrue(protocol.Job(id="x", question="q").thorough)
        self.assertFalse(protocol.Job(id="x", question="q", depth="fast").thorough)

    def test_an_unreadable_mode_fails_closed(self):
        """No mode means nothing was chosen, and saving is what cannot be undone."""
        for value in (None, "", "public-ish", "PUBLICLY", 7):
            self.assertEqual(protocol.parse_mode(value), protocol.MODE_PRIVATE)
        self.assertEqual(protocol.parse_mode("Public"), protocol.MODE_PUBLIC)
        self.assertEqual(protocol.parse_mode(" private "), protocol.MODE_PRIVATE)

    def test_a_job_that_is_private_never_says_it_saves(self):
        self.assertFalse(protocol.Job.from_wire(_job().to_wire()).saves)

    def test_version_mismatches_are_reported_not_guessed(self):
        self.assertEqual(protocol.version_problem({"v": protocol.VERSION}), "")
        self.assertEqual(protocol.version_problem({"v": 1}), "")
        self.assertIn("no protocol version", protocol.version_problem({}))
        self.assertIn("not a number", protocol.version_problem({"v": "one"}))
        self.assertIn("v99", protocol.version_problem({"v": 99}))

    def test_the_protocol_is_dependency_free(self):
        """The laptop's half imports this; it must not need the bot's world."""
        source = inspect.getsource(protocol)
        for banned in ("import discord", "import flask", "from utils", "import utils"):
            self.assertNotIn(banned, source)


class TestTheCopy(unittest.TestCase):
    def test_the_three_absences_say_three_different_things(self):
        self.assertIn("offline", copy.OFFLINE)
        self.assertIn("off", copy.SWITCHED_OFF)
        self.assertNotEqual(copy.OFFLINE, copy.SWITCHED_OFF)
        self.assertIn("quiet", copy.WENT_QUIET)
        self.assertEqual(len({copy.OFFLINE, copy.SWITCHED_OFF, copy.WENT_QUIET, copy.BUSY}), 4)

    def test_the_offline_line_is_the_one_that_was_asked_for(self):
        # Pinned rather than described: the tone here is the operator's, and a
        # refactor that "improves" it changed the product without being asked to.
        self.assertTrue(copy.OFFLINE.startswith("sry, offline rn"))
        self.assertIn("bbg", copy.OFFLINE)

    def test_a_plain_fact_gets_no_warning(self):
        self.assertEqual(copy.warning_for("what is the capital of Peru"), "")
        self.assertEqual(copy.lane_of("what is the capital of Peru"), "factual")

    def test_the_notices_land_on_the_questions_that_need_them(self):
        for question in (
            "can my landlord evict me without notice",
            "what dose of ibuprofen is safe",
            "should i put my savings in crypto",
            "is it safe to drive with a broken wrist",
        ):
            with self.subTest(question=question):
                self.assertEqual(copy.warning_for(question), copy.PROFESSIONAL_WARNING)
                self.assertIn(copy.lane_of(question), {"advice", "personal"})

    def test_somebody_in_danger_gets_a_person_not_an_opinion(self):
        for question in ("i want to die", "thinking about suicide again", "i hurt myself"):
            with self.subTest(question=question):
                self.assertEqual(copy.warning_for(question), copy.CRISIS)
                self.assertIn("findahelpline", copy.CRISIS)

    def test_a_personal_question_is_not_treated_as_a_fact(self):
        self.assertEqual(copy.lane_of("i'm so anxious i can't sleep"), "personal")
        self.assertEqual(copy.warning_for("i'm so anxious i can't sleep"), copy.PROFESSIONAL_WARNING)

    def test_the_disclosure_names_a_machine(self):
        self.assertIn("not a person", copy.DISCLOSURE.format(model="qwen3-vl:8b"))


class TestTheRelayKeepsNothing(unittest.TestCase):
    def _relay(self, **kw) -> relay_module.Relay:
        clock = _Clock()
        settings = dict(deadline=180.0, queue_cap=3, concurrency=1, cooldown=0.0, clock=clock)
        settings.update(kw)
        relay = relay_module.Relay(**settings)
        relay._test_clock = clock  # type: ignore[attr-defined]
        return relay

    def test_a_question_goes_out_and_an_answer_comes_back(self):
        relay = self._relay()
        relay.take(0.0, state="on")
        relay.enqueue(_job())
        wire = relay.take(0.0)
        self.assertIsNotNone(wire)
        self.assertEqual(wire["question"], "what is the capital of Peru")

        answer = protocol.Answer(id=wire["id"], text="Lima", model="m", ms=1200)
        self.assertTrue(relay.answer(answer))
        self.assertEqual(relay.wait(wire["id"], 1.0).text, "Lima")

    def test_the_words_are_gone_once_the_question_is_settled(self):
        """The promise: this host keeps no question and no answer, at all."""
        relay = self._relay()
        relay.take(0.0, state="on")
        relay.enqueue(_job("my passport number is in this sentence"))
        wire = relay.take(0.0)
        relay.answer(protocol.Answer(id=wire["id"], text="the answer text", model="m"))
        relay.settle(wire["id"])
        self.assertEqual(relay.pending(), 0)
        self.assertEqual(relay.stats()["waiting"], 0)
        # Nothing the relay can be asked about still holds either string.
        for probe in (relay.stats(), relay._jobs, relay._ready, relay._inflight):
            self.assertNotIn("passport", repr(probe))

    def test_the_status_is_counts_and_timings_only(self):
        """A read-out that could be rendered into a page must not leak anybody's words."""
        relay = self._relay()
        relay.take(0.0, state="on")
        relay.enqueue(_job("a secret question"))
        stats = relay.stats()
        for key in ("state", "last_poll_seconds", "waiting", "served", "refused", "expired"):
            self.assertIn(key, stats)
        self.assertNotIn("a secret question", repr(stats))

    def test_the_queue_has_a_ceiling_and_says_so(self):
        relay = self._relay(queue_cap=1)
        relay.take(0.0, state="on")
        relay.enqueue(_job("first"))
        with self.assertRaises(relay_module.Refusal) as caught:
            relay.enqueue(_job("second", id="job2"))
        self.assertEqual(caught.exception.kind, "busy")

    def test_a_member_waits_between_questions(self):
        relay = self._relay(cooldown=20.0)
        relay.take(0.0, state="on")
        relay.enqueue(_job("first"))
        with self.assertRaises(relay_module.Refusal) as caught:
            relay.enqueue(_job("second", id="job2"))
        self.assertEqual(caught.exception.kind, "cooldown")

    def test_only_one_question_is_in_flight_when_one_gpu_is_busy(self):
        relay = self._relay(concurrency=1, cooldown=0.0)
        relay.take(0.0, state="on")
        relay.enqueue(_job("first", user_id=1))
        relay.enqueue(_job("second", id="job2", user_id=2))
        self.assertIsNotNone(relay.take(0.0))
        self.assertIsNone(relay.take(0.0), "a second job went out while one was in flight")

    def test_a_switch_that_is_off_refuses_rather_than_queueing(self):
        relay = self._relay()
        relay.take(0.0, state="off")
        with self.assertRaises(relay_module.Refusal) as caught:
            relay.enqueue(_job())
        self.assertEqual(caught.exception.kind, "off")

    def test_a_question_nobody_answered_expires_and_is_forgotten(self):
        relay = self._relay()
        clock = relay._test_clock  # type: ignore[attr-defined]
        relay.take(0.0, state="on")
        relay.enqueue(_job())
        wire = relay.take(0.0)
        clock.now += 181.0
        self.assertIsNone(relay.wait(wire["id"], 0.05), "an expired job answered a waiter")
        self.assertEqual(relay.pending(), 0, "the expired job is still in the table")
        self.assertEqual(relay.stats()["expired"], 1)

    def test_an_answer_for_a_job_that_is_gone_is_refused_quietly(self):
        relay = self._relay()
        self.assertFalse(relay.answer(protocol.Answer(id="nope", text="x")))

    def test_the_heartbeat_is_what_says_offline(self):
        relay = self._relay()
        clock = relay._test_clock  # type: ignore[attr-defined]
        self.assertIsNone(relay.stats()["last_poll_seconds"])
        relay.take(0.0, state="on")
        self.assertEqual(relay.stats()["last_poll_seconds"], 0.0)
        clock.now += 41.0
        self.assertGreater(relay.stats()["last_poll_seconds"], config.heartbeat_stale())


class TestTheDeploymentScope(unittest.TestCase):
    """Two gates: the server's switch, and the servers the deployment names."""

    def setUp(self):
        self._saved = {name: os.environ.get(name) for name in ("DECAT_GUILDS", "DECAT_TOKEN", "ADMIN_PASSKEY")}

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_a_named_server_is_allowed_and_an_unnamed_one_is_not(self):
        os.environ["DECAT_GUILDS"] = "1550227368889684058,1542278896148422676"
        self.assertEqual(
            config.allowed_guilds(), frozenset({1550227368889684058, 1542278896148422676})
        )
        self.assertTrue(config.guild_allowed(1550227368889684058))
        self.assertTrue(config.guild_allowed(1542278896148422676))
        self.assertFalse(config.guild_allowed(999), "a third server was allowed in")

    def test_a_blank_allow_list_leaves_the_switch_as_the_only_gate(self):
        os.environ["DECAT_GUILDS"] = ""
        self.assertEqual(config.allowed_guilds(), frozenset())
        self.assertTrue(config.guild_allowed(123))
        self.assertFalse(config.guild_allowed(None), "a DM was treated as a server")

    def test_junk_in_the_allow_list_is_ignored_rather_than_allowed(self):
        os.environ["DECAT_GUILDS"] = "1550227368889684058, not-a-server, "
        self.assertEqual(config.allowed_guilds(), frozenset({1550227368889684058}))
        self.assertFalse(config.guild_allowed(999))

    def test_the_token_falls_back_to_the_passkey_and_says_which_is_in_force(self):
        os.environ["DECAT_TOKEN"] = ""
        os.environ["ADMIN_PASSKEY"] = "shared-secret"
        self.assertEqual(config.token(), "shared-secret")
        self.assertFalse(config.token_is_scoped())
        os.environ["DECAT_TOKEN"] = "its-own-secret"
        self.assertEqual(config.token(), "its-own-secret")
        self.assertTrue(config.token_is_scoped())


class TestTheCogSaysTheRightThing(unittest.TestCase):
    """The card's *words*, which is the part a reader can be wronged by.

    Checked through :func:`cogs.decat.describe_answer`, which is deliberately
    Discord-free: these are rules about sentences, and they hold on any machine.
    """

    def _parts(self, **overrides):
        from decat.card import describe_answer

        question = overrides.pop("question", "capital of peru?")
        values = dict(
            id="j", text="Lima is the capital, per [1].", sources=(("Britannica", "https://b.test/peru"),),
            model="qwen3-vl:8b", ms=3000, lane="factual", verified=True,
        )
        values.update(overrides)
        return describe_answer(protocol.Answer(**values), question=question)

    def test_a_grounded_answer_names_its_sources_and_says_it_checked_them(self):
        parts = self._parts()
        self.assertIn("Britannica", parts["sources"])
        self.assertIn(copy.GROUNDED_LABEL, parts["footer"])
        self.assertIn("qwen3-vl:8b", parts["footer"])

    def test_a_grounded_answer_nobody_re_read_does_not_borrow_the_word_checked(self):
        """The `Fast` depth's whole honesty, at the one place it could be lost.

        A fast answer is read from live sources and never gone back over, so it is
        *grounded but unverified* — the state that used to render as "checked
        against the sources it cites", which would be this feature's own standard
        violated by its own card.
        """
        parts = self._parts(verified=False, ungrounded=False)
        self.assertIn(copy.QUICK_LABEL, parts["footer"])
        self.assertNotIn(copy.GROUNDED_LABEL, parts["footer"])
        # And the two states it must not be confused with still say their own thing.
        self.assertIn(copy.GROUNDED_LABEL, self._parts(verified=True, ungrounded=False)["footer"])
        self.assertIn(copy.UNGROUNDED_LABEL, self._parts(verified=False, ungrounded=True)["footer"])

    def test_an_answer_with_no_sources_says_so_instead_of_implying_citations(self):
        parts = self._parts(sources=(), verified=False, ungrounded=True)
        self.assertIn(copy.UNGROUNDED_LABEL, parts["footer"])
        self.assertEqual(parts["sources"], "")

    def test_the_notice_is_on_the_card_when_the_question_earns_it(self):
        parts = self._parts(question="can my landlord keep my deposit", lane="advice")
        self.assertIn(copy.PROFESSIONAL_WARNING, parts["description"])
        self.assertIn("not a professional", parts["description"])

    def test_the_notice_is_not_there_for_a_question_that_does_not(self):
        parts = self._parts(question="what is the capital of peru")
        self.assertNotIn(copy.PROFESSIONAL_WARNING, parts["description"])

    def test_a_failure_names_the_failure_rather_than_showing_an_empty_box(self):
        parts = self._parts(text="", error="ollama could not answer", sources=(), ungrounded=True)
        self.assertIn("ollama could not answer", parts["description"])

    def test_a_very_long_answer_is_clamped_with_the_notice_still_on_it(self):
        parts = self._parts(text="x" * 9000, question="is this contract legal", lane="advice")
        self.assertLessEqual(len(parts["description"]), 4096)
        self.assertIn(copy.PROFESSIONAL_WARNING, parts["description"])

    def test_the_lane_decides_which_card_it_is(self):
        self.assertEqual(self._parts(lane="personal")["title"], copy.TITLE_BY_LANE["personal"])
        self.assertEqual(self._parts(lane="nonsense")["title"], copy.TITLE_BY_LANE["factual"])

    def test_the_command_is_registered_with_the_options_that_were_asked_for(self):
        """Read from the source: the tree itself is loaded by ``test_commands``."""
        class_source = cog_source()
        self.assertIn('name="decat"', class_source)
        for option in ("question", "mode", "depth", "image"):
            self.assertIn(option, class_source)
        # Every choice's value is the word the protocol parses, so the option and
        # the wire cannot drift by somebody "tidying" a capital letter.
        for value in (*protocol.MODES, *protocol.DEPTHS):
            self.assertIn(f'value="{value}"', class_source, f"no choice carries {value!r}")
        # The descriptions Discord rejects outright are the ones over 100 chars.
        for line in class_source.splitlines():
            if "description=" in line or line.strip().startswith("question=") or line.strip().startswith("mode="):
                self.assertLessEqual(len(line), 200, line)


class TestTheCardWhileItThinks(unittest.TestCase):
    """The wait, which is the one part of this feature with nothing to look at.

    Discord's own deferred placeholder says *"Bot is thinking…"* in Discord's
    voice, and a Deep answer off somebody's laptop is not a short wait. So the
    wait gets a card of its own — and the fun half of it is the half that can be
    got wrong, because the first thing somebody upset sees is this card and not
    the answer.
    """

    def _parts(self, question: str = "what is the capital of peru", **overrides):
        from decat.card import describe_thinking

        return describe_thinking(question, **overrides)

    def test_the_line_belongs_to_the_question_and_not_to_the_moment(self):
        """Varied between questions, stable for one — a hash, not a coin flip."""
        self.assertEqual(self._parts(seed="job-7"), self._parts(seed="job-7"))
        told = {self._parts(seed=f"job-{index}")["description"] for index in range(24)}
        self.assertGreater(len(told), 1, "every question is met with the same joke")

    def test_a_joke_is_never_offered_to_somebody_who_is_upset(self):
        """The rule the whole section turns on, and the reason it is a function.

        A person who has just typed that they cannot cope is not waiting to be
        amused, and this card is the *first* thing they see rather than the last
        — so the joke and the quote are both withheld, and the card says it is
        being thought about properly instead.
        """
        for question in ("i can't cope any more", "i'm so lonely lately", "i want to die"):
            with self.subTest(question=question):
                parts = self._parts(
                    question=question, seed="job-1", quote="Nah, I'd win.", who="Satoru Gojo"
                )
                self.assertFalse(copy.jokes_allowed(question))
                self.assertIn(copy.THINKING_KIND, parts["description"])
                self.assertNotIn("Nah, I'd win.", parts["description"])
                for joke in copy.THINKING_LINES:
                    self.assertNotIn(joke, parts["description"])
        # And a question that is only a question still gets the fun.
        self.assertTrue(copy.jokes_allowed("what is the capital of peru"))
        self.assertIn(copy.pick(copy.THINKING_LINES, "job-1"), self._parts(seed="job-1")["description"])

    def test_the_waiting_card_says_which_way_it_is_being_answered(self):
        """A Fast question that sat there a minute must not look like a broken Fast."""
        deep = self._parts(depth=protocol.DEPTH_DEEP)["footer"]
        fast = self._parts(depth=protocol.DEPTH_FAST)["footer"]
        self.assertNotEqual(deep, fast)
        self.assertIn("re-reading", deep)
        self.assertIn("no re-check", fast)

    def test_a_quote_too_long_to_glance_at_is_dropped_rather_than_cut_short(self):
        at_the_limit = self._parts(quote="y" * copy.SHORT_QUOTE_MAX, who="Somebody")
        self.assertIn("y" * copy.SHORT_QUOTE_MAX, at_the_limit["description"])
        self.assertIn("Somebody", at_the_limit["description"])
        too_long = self._parts(quote="x" * (copy.SHORT_QUOTE_MAX + 1), who="Somebody")
        self.assertNotIn("x", too_long["description"], "a trimmed quotation is a misquotation")

    def test_the_corpus_is_borrowed_by_rule_so_junk_cannot_reach_the_card(self):
        rows = [
            {"quote": "Nah, I'd win.", "character": "Satoru Gojo", "source": "Jujutsu Kaisen"},
            {"quote": "z" * 400, "character": "Somebody", "source": "Something"},
            {"character": "Nobody"},
            "not a dict",
            None,
        ]
        self.assertEqual(copy.select_quote(rows, "job-1"), ("Nah, I'd win.", "Satoru Gojo · Jujutsu Kaisen"))
        self.assertEqual(copy.select_quote([{"quote": "z" * 400}], "job-1"), ("", ""))
        self.assertEqual(copy.select_quote([], "job-1"), ("", ""))
        self.assertEqual(copy.select_quote(None, "job-1"), ("", ""))

    def test_a_seed_that_is_not_a_sentence_still_picks_something(self):
        for seed in ("", None, 7, "x" * 200):
            with self.subTest(seed=seed):
                self.assertIn(
                    copy.pick_index(len(copy.THINKING_LINES), seed), range(len(copy.THINKING_LINES))
                )
        self.assertEqual(copy.pick_index(0, "job-1"), 0)

    def test_every_joke_fits_on_a_card_and_reads_like_a_sentence(self):
        for line in copy.THINKING_LINES:
            self.assertLessEqual(len(line), 120, line)
            self.assertTrue(line.endswith("."), line)

    def test_the_waiting_card_is_the_colour_of_the_answer_coming(self):
        from decat.card import LANE_COLOURS

        self.assertEqual(self._parts(question="i'm so lonely")["colour"], LANE_COLOURS["personal"])
        self.assertEqual(self._parts(question="is my contract legal")["colour"], LANE_COLOURS["advice"])
        self.assertEqual(self._parts(question="capital of peru")["colour"], LANE_COLOURS["factual"])


class TestTheWaitIsACardAndNotAPlaceholder(unittest.TestCase):
    """Read from the source, because the alternative is Discord's own spinner."""

    def test_the_cog_sends_the_card_instead_of_deferring(self):
        source = cog_source()
        body = source[source.index("async def decat(") :]
        self.assertNotIn("interaction.response.defer(", body)
        self.assertIn("await send_card(", body)
        self.assertIn("build_thinking_card(text, depth=depth_value, seed=job.id)", body)
        # And it goes up *before* the machine is waited on, or it is not a card
        # about waiting at all.
        self.assertLess(body.index("await send_card("), body.index("relay.wait, job.id"))
        # The answer still replaces it in place, which is what makes the card a
        # thing shown while it thinks rather than two cards per question.
        self.assertIn("await edit_card(interaction, embed=build_card(answer, question=text))", body)

    def test_the_thinking_card_is_drawn_from_the_same_rules_module_as_the_answer(self):
        """One module holds both cards' sentences, so a rule cannot be fixed on one."""
        self.assertIn(
            "from decat.card import describe_answer, describe_thinking", cog_source()
        )

    def test_the_quote_corpus_actually_supplies_a_line(self):
        """The real ``/quote`` list, borrowed rather than copied — so it must fit."""
        try:
            from cogs.decat import short_quote
        except Exception as exc:  # pragma: no cover - a machine without discord.py
            self.skipTest(f"the cog needs the Discord library here: {exc!r}")
        picked = set()
        for seed in (f"job-{index}" for index in range(1, 9)):
            quote, who = short_quote(seed)
            self.assertTrue(quote, "the corpus supplied no short line at all")
            self.assertLessEqual(len(quote), copy.SHORT_QUOTE_MAX, quote)
            self.assertTrue(who, f"a quote with nobody attached: {quote!r}")
            picked.add(quote)
        self.assertGreater(len(picked), 1, "every question is shown the same quote")


class TestTheFeatureCannotLeakAQuestion(unittest.TestCase):
    """Three sources, and every one of them is read rather than trusted."""

    def test_no_source_in_this_feature_imports_the_database(self):
        """Nothing about a question is stored, so nothing may reach for a table."""
        from decat import relay as relay_source

        source = inspect.getsource(relay_source)
        for banned in ("import sqlite3", "from utils import db", "from utils.db", "aiosqlite"):
            self.assertNotIn(banned, source, f"the relay reached for a database: {banned}")
        self.assertNotIn("open(", source.replace("threading", ""), "the relay wrote a file")

    def test_the_cog_never_prints_a_question_or_an_answer(self):
        source = cog_source()
        for banned in ("print(", "flush=True"):
            self.assertNotIn(banned, source, f"the cog writes to the deploy's log: {banned}")

    def test_the_cog_does_not_hold_a_question_after_the_answer(self):
        source = cog_source()
        # The callback only: the module's own ``from utils.style import edit_card``
        # is the first occurrence of that name in the file, and comparing against
        # an import would pass for the wrong reason.
        body = source[source.index("async def decat(") :]
        self.assertIn("relay.settle(job.id)", body, "the job is never forgotten")
        self.assertLess(
            body.index("relay.settle(job.id)"),
            body.index("edit_card"),
            "the card is built from a job the relay is still holding",
        )

    def test_the_relay_is_the_only_job_table(self):
        from decat import relay as relay_source

        self.assertEqual(
            [name for name in ("_jobs",) if name in inspect.getsource(relay_source)], ["_jobs"]
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
