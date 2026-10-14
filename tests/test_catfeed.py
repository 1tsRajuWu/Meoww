"""The cat's outside world: fetched, cleaned, and never required.

Three providers are involved and every one of them is somebody else's free
service, which is the reason this file exists rather than a comment: a provider
can change its payload shape, rate-limit, 404 or vanish, and the failure that
matters is not the outage — it is a *card*. So the laws here are about the two
ways this can go wrong quietly:

* **A payload read loosely.** ``fact_text`` reads through the source table, so a
  provider that renames a field reads as "no fact" instead of raising inside a
  command. That is pinned shape by shape.
* **A model's words reaching people.** Everything a model writes goes through
  ``clean``, which removes mentions and links and refuses empty. Its rules are
  laws rather than intentions because a cat that can be talked into pinging
  ``@everyone`` is a cat that should not have been given a model.

The network is faked here for the same reason ``tests/test_gifs.py`` fakes it:
the shapes are what is under test, and a suite that calls the real APIs fails
whenever somebody else's server has a bad afternoon.
"""
import os
import unittest
from unittest import mock

from utils import catfeed, config_audit

CATFACT = "https://catfact.ninja/fact"
MEOWFACTS = "https://meowfacts.herokuapp.com/"
THE_CAT_API = "https://api.thecatapi.com/v1/images/search"
GOOD_PICTURE = "https://cdn2.thecatapi.com/images/bkp.jpg"

#: Absent, explicitly: the keyless provider is what a fresh checkout uses, and a
#: developer's own ``.env`` must not be able to change what this suite proves.
KEYLESS = {"CAT_AI_KEY": "", "CAT_AI_URL": "", "CAT_AI_MODEL": ""}


def _json(payload, status=200):
    return {"status": status, "payload": payload, "content_type": "application/json"}


class _Response:
    def __init__(self, spec):
        self.status = spec.get("status", 200)
        self.headers = {"Content-Type": spec.get("content_type", "application/json")}
        self._payload = spec.get("payload")
        self.released = False

    async def json(self, *args, **kwargs):
        return self._payload

    def release(self):
        self.released = True


class _Session:
    """A fake aiohttp session: a URL-to-response table that records every call."""

    def __init__(self, routes):
        self.routes = {url: list(specs) for url, specs in routes.items()}
        self.calls = []

    def _answer(self, url, kwargs):
        self.calls.append({"method": self._method, "url": url, **kwargs})
        specs = self.routes.get(url)
        if not specs:
            return _Response({"status": 404, "content_type": "text/html"})
        spec = specs[0] if len(specs) == 1 else specs.pop(0)
        if isinstance(spec, Exception):
            raise spec
        return _Response(spec)

    async def get(self, url, **kwargs):
        self._method = "GET"
        return self._answer(url, kwargs)

    async def post(self, url, **kwargs):
        self._method = "POST"
        return self._answer(url, kwargs)

    def hits(self, url):
        return sum(1 for call in self.calls if call["url"] == url)


def _answer(text):
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": text}}]}


class TheProvidersAreTheOnesWeLeantOn(unittest.TestCase):
    def setUp(self):
        catfeed.clear_caches()

    def test_every_fact_source_is_https_and_knows_its_own_shape(self):
        self.assertTrue(catfeed.FACT_SOURCES)
        for spec in catfeed.FACT_SOURCES:
            self.assertTrue(spec["url"].startswith("https://"), spec["url"])
            self.assertTrue(spec["id"])
            self.assertTrue(callable(spec["pick"]))

    def test_a_payload_is_only_read_where_a_fact_could_be(self):
        self.assertEqual(catfeed.fact_text({"fact": "Cats purr."}, "catfact"), "Cats purr.")
        self.assertEqual(catfeed.fact_text({"data": ["Cats nap."]}, "meowfacts"), "Cats nap.")
        # A provider that renames a field is *no fact*, never an exception: this is
        # the read that used to be a bare ``payload["fact"]``.
        self.assertIsNone(catfeed.fact_text({"renamed": "x"}, "catfact"))
        self.assertIsNone(catfeed.fact_text(None, "catfact"))
        self.assertIsNone(catfeed.fact_text({"fact": "x"}, "nobody"))
        self.assertIsNone(catfeed.fact_text({"fact": "   "}, "catfact"))
        self.assertIsNone(catfeed.fact_text({"data": []}, "meowfacts"))
        self.assertIsNone(catfeed.fact_text({"fact": "x" * 601}, "catfact"))

    def test_the_two_brains_are_different_endpoints(self):
        self.assertTrue(catfeed.DEFAULT_AI_URL.startswith("https://"))
        self.assertTrue(catfeed.PUBLIC_AI_URL.startswith("https://"))
        self.assertNotEqual(catfeed.DEFAULT_AI_URL, catfeed.PUBLIC_AI_URL)

    def test_the_key_decides_which_brain_answers(self):
        with mock.patch.dict(os.environ, KEYLESS, clear=False):
            self.assertEqual(catfeed.provider_id(), "public")
            self.assertEqual([spec["id"] for spec in catfeed.brain_chain()], ["public"])
        with mock.patch.dict(os.environ, {"CAT_AI_KEY": "a-key"}):
            self.assertEqual(catfeed.provider_id(), "configured")
            self.assertEqual(
                [spec["id"] for spec in catfeed.brain_chain()], ["configured", "public"]
            )

    def test_an_endpoint_and_a_model_can_be_chosen(self):
        with mock.patch.dict(
            os.environ,
            {"CAT_AI_KEY": "k", "CAT_AI_URL": "https://local.example/v1/chat/completions", "CAT_AI_MODEL": "llama"},
        ):
            endpoint = catfeed.ai_endpoint()
        self.assertEqual(endpoint["url"], "https://local.example/v1/chat/completions")
        self.assertEqual(endpoint["model"], "llama")

    def test_the_settings_it_reads_are_declared(self):
        """Read through ``config_audit`` or the name is invisible to the operator.

        ``tests/test_config_audit.py`` owns the other half of this (a declaration
        nothing reads fails there); this is the half that says the brain's three
        names are the declared ones rather than strings spelled here twice.
        """
        declared = {setting.name for setting in config_audit.SETTINGS}
        for name in ("CAT_AI_KEY", "CAT_AI_URL", "CAT_AI_MODEL"):
            self.assertIn(name, declared)


class WhatAModelWritesIsCleanedBeforeItIsPosted(unittest.TestCase):
    def test_quotes_and_fences_are_the_model_s_formatting_not_the_cat_talking(self):
        self.assertEqual(catfeed.clean('"I like tuna."'), "I like tuna.")
        self.assertEqual(catfeed.clean("`mrrp`"), "mrrp")
        self.assertEqual(catfeed.clean("\u201cI like tuna.\u201d"), "I like tuna.")

    def test_every_mention_is_removed(self):
        cleaned = catfeed.clean("<@123> <@!123> <@&456> <#789> @everyone @Here look")
        self.assertEqual(cleaned, "look")

    def test_links_are_removed(self):
        self.assertEqual(catfeed.clean("go to https://evil.example/x now"), "go to now")
        self.assertEqual(catfeed.clean("and www.evil.example too"), "and too")

    def test_whitespace_is_collapsed(self):
        self.assertEqual(catfeed.clean("  two\n\nlines\tand  spaces "), "two lines and spaces")

    def test_nothing_usable_is_none_rather_than_an_empty_card(self):
        for value in ("", "   ", '"', "https://evil.example/x", "<@123>", None):
            self.assertIsNone(catfeed.clean(value), repr(value))

    def test_a_long_sentence_is_cut_at_a_word_and_says_so(self):
        cleaned = catfeed.clean("word " * 200, limit=40)
        self.assertTrue(cleaned.endswith("\u2026"))
        self.assertLessEqual(len(cleaned), 41)
        self.assertNotIn("wor\u2026", cleaned)

    def test_the_limit_is_the_callers(self):
        cleaned = catfeed.clean("one two three four five", limit=7)
        self.assertTrue(len(cleaned) <= 8, cleaned)


class ReadingAChatCompletion(unittest.TestCase):
    def test_the_answer_is_the_message(self):
        self.assertEqual(catfeed.reply_text(_answer("Tuna.")), "Tuna.")

    def test_the_models_working_is_not_the_answer(self):
        payload = {
            "choices": [
                {"message": {"content": "Tuna.", "reasoning": "the user asked about tuna"}}
            ]
        }
        self.assertEqual(catfeed.reply_text(payload), "Tuna.")

    def test_junk_reads_as_no_answer(self):
        for payload in ({}, {"choices": []}, {"choices": [{}]}, {"choices": [{"message": {}}]},
                        {"choices": [{"message": {"content": ""}}]}, None, "not json"):
            self.assertIsNone(catfeed.reply_text(payload), repr(payload))


class FetchingAFact(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        catfeed.clear_caches()

    async def test_a_provider_that_answers_is_used(self):
        session = _Session({CATFACT: [_json({"fact": "A real fact."})]})
        self.assertEqual(await catfeed.fact(session=session), "A real fact.")
        self.assertLessEqual(session.hits(CATFACT), 1)

    async def test_a_provider_that_answers_garbage_falls_through(self):
        session = _Session({
            CATFACT: [_json({"renamed": "x"})],
            MEOWFACTS: [_json({"data": ["The back-up fact."]})],
        })
        self.assertEqual(await catfeed.fact(session=session), "The back-up fact.")

    async def test_the_same_fact_twice_is_refused_in_favour_of_the_next_provider(self):
        session = _Session({
            CATFACT: [_json({"fact": "Same."}), _json({"fact": "Same."})],
            MEOWFACTS: [_json({"data": ["Different."]})],
        })
        self.assertEqual(await catfeed.fact(session=session), "Same.")
        self.assertEqual(await catfeed.fact(session=session), "Different.")

    async def test_a_repeat_is_better_than_no_card_when_every_provider_is_out_of_facts(self):
        session = _Session({CATFACT: [_json({"fact": "Same."})]})
        self.assertEqual(await catfeed.fact(session=session), "Same.")
        self.assertEqual(await catfeed.fact(session=session), "Same.")

    async def test_every_provider_down_is_none(self):
        session = _Session({})
        self.assertIsNone(await catfeed.fact(session=session))

    async def test_a_provider_that_just_failed_is_left_alone(self):
        session = _Session({CATFACT: [_json({"broken": True})]})
        self.assertIsNone(await catfeed.fact(session=session, now=1000.0))
        self.assertIsNone(await catfeed.fact(session=session, now=1001.0))
        self.assertEqual(session.hits(CATFACT), 1, "a dead provider must not be retried per call")

    async def test_a_request_that_raises_is_a_missing_fact_not_a_broken_command(self):
        session = _Session({CATFACT: [OSError("no route to host")]})
        self.assertIsNone(await catfeed.fact(session=session))

    async def test_a_non_200_is_a_missing_fact(self):
        session = _Session({CATFACT: [_json({"fact": "x"}, status=503)]})
        self.assertIsNone(await catfeed.fact(session=session))

    async def test_the_lookup_has_a_timeout(self):
        session = _Session({CATFACT: [_json({"fact": "A fact."})]})
        await catfeed.fact(session=session)
        self.assertEqual(session.calls[0].get("timeout"), catfeed.REQUEST_TIMEOUT)


class AskingTheBrain(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        catfeed.clear_caches()

    async def test_with_no_key_the_public_model_answers_and_nothing_is_authorised(self):
        session = _Session({catfeed.PUBLIC_AI_URL: [_json(_answer("Tuna."))]})
        with mock.patch.dict(os.environ, KEYLESS, clear=False):
            self.assertEqual(await catfeed.say([{"role": "user", "content": "?"}], session=session), "Tuna.")
        call = session.calls[0]
        self.assertEqual(call["url"], catfeed.PUBLIC_AI_URL)
        self.assertNotIn("Authorization", call.get("headers") or {})
        self.assertEqual(call["json"]["model"], catfeed.PUBLIC_AI_MODEL)
        self.assertEqual(call.get("timeout"), catfeed.BRAIN_TIMEOUT)

    async def test_with_a_key_the_configured_endpoint_is_called_with_a_bearer(self):
        session = _Session({catfeed.DEFAULT_AI_URL: [_json(_answer("Tuna."))]})
        with mock.patch.dict(os.environ, {"CAT_AI_KEY": "secret-key"}):
            said = await catfeed.say([{"role": "user", "content": "?"}], session=session)
        self.assertEqual(said, "Tuna.")
        call = session.calls[0]
        self.assertEqual(call["url"], catfeed.DEFAULT_AI_URL)
        self.assertEqual(call["headers"]["Authorization"], "Bearer secret-key")
        self.assertEqual(call["json"]["model"], catfeed.DEFAULT_AI_MODEL)

    async def test_a_key_that_does_not_work_falls_through_to_the_public_model(self):
        session = _Session({
            catfeed.DEFAULT_AI_URL: [_json({"error": "quota"}, status=429)],
            catfeed.PUBLIC_AI_URL: [_json(_answer("Still here."))],
        })
        with mock.patch.dict(os.environ, {"CAT_AI_KEY": "expired"}):
            said = await catfeed.say([{"role": "user", "content": "?"}], session=session)
        self.assertEqual(said, "Still here.")
        self.assertEqual(session.hits(catfeed.PUBLIC_AI_URL), 1)

    async def test_both_brains_down_is_none_rather_than_a_raised_error(self):
        session = _Session({})
        with mock.patch.dict(os.environ, KEYLESS, clear=False):
            self.assertIsNone(await catfeed.say([{"role": "user", "content": "?"}], session=session))

    async def test_a_request_that_raises_is_no_answer(self):
        session = _Session({catfeed.PUBLIC_AI_URL: [TimeoutError("too slow")]})
        with mock.patch.dict(os.environ, KEYLESS, clear=False):
            self.assertIsNone(await catfeed.say([{"role": "user", "content": "?"}], session=session))

    async def test_an_answer_with_no_message_is_no_answer(self):
        session = _Session({catfeed.PUBLIC_AI_URL: [_json({"choices": []})]})
        with mock.patch.dict(os.environ, KEYLESS, clear=False):
            self.assertIsNone(await catfeed.say([{"role": "user", "content": "?"}], session=session))

    async def test_nothing_a_model_writes_reaches_a_caller_uncleaned(self):
        """The one door. A model that answers with a ping does not get to send one."""
        session = _Session({
            catfeed.PUBLIC_AI_URL: [_json(_answer("@everyone visit https://evil.example"))]
        })
        with mock.patch.dict(os.environ, KEYLESS, clear=False):
            said = await catfeed.say([{"role": "user", "content": "?"}], session=session)
        self.assertEqual(said, "visit")

    async def test_an_answer_that_cleans_to_nothing_is_no_answer(self):
        session = _Session({catfeed.PUBLIC_AI_URL: [_json(_answer("@everyone"))]})
        with mock.patch.dict(os.environ, KEYLESS, clear=False):
            self.assertIsNone(await catfeed.say([{"role": "user", "content": "?"}], session=session))

    async def test_the_temperature_is_the_callers(self):
        session = _Session({catfeed.PUBLIC_AI_URL: [_json(_answer("Sure."))]})
        with mock.patch.dict(os.environ, KEYLESS, clear=False):
            await catfeed.say([{"role": "user", "content": "?"}], session=session, temperature=0.1)
        self.assertEqual(session.calls[0]["json"]["temperature"], 0.1)

    async def test_a_provider_that_just_failed_is_left_alone(self):
        session = _Session({catfeed.PUBLIC_AI_URL: [_json({"choices": []}, status=500)]})
        with mock.patch.dict(os.environ, KEYLESS, clear=False):
            self.assertIsNone(await catfeed.say([{"role": "user", "content": "?"}], session=session))
            self.assertIsNone(await catfeed.say([{"role": "user", "content": "?"}], session=session))
        self.assertEqual(session.hits(catfeed.PUBLIC_AI_URL), 1)


class ThePictureComesFromTheResolverThatAlreadyExists(unittest.IsolatedAsyncioTestCase):
    """``/cat photo`` is not a second picture pipeline — it is the soundboard's."""

    def setUp(self):
        from utils import gifs

        gifs.clear_caches()

    async def test_the_cat_sources_include_a_second_provider(self):
        from utils import gifs

        self.assertGreaterEqual(len(gifs.PET_SOURCES["cat"]), 3)
        urls = [spec["url"] for spec in gifs.PET_SOURCES["cat"]]
        self.assertIn(THE_CAT_API, urls)

    async def test_a_picture_needs_the_address_the_api_gave_and_a_real_image_behind_it(self):
        from utils import gifs

        session = _Session({
            THE_CAT_API: [_json([{"url": GOOD_PICTURE}])],
            GOOD_PICTURE: [_json(None, status=200) | {"content_type": "image/jpeg"}],
        })
        # The first two cat sources are the animated one and the photo the Cat API
        # hands back; whichever answers, the answer is a URL that was probed.
        url = await catfeed.photo(session=session)
        self.assertIn(url, {GOOD_PICTURE, "https://cataas.com/cat/gif", "https://cataas.com/cat"})

    async def test_no_provider_answering_is_none(self):
        self.assertIsNone(await catfeed.photo(session=_Session({})))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
