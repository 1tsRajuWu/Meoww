"""Pictures are fetched and *verified*, because a hardcoded URL rots silently.

Every roleplay card used to carry hand-typed ``media.tenor.com`` links, and every
one of them answered 404 by the time anybody looked — which Discord renders as an
empty box. The bot cannot tell the difference between "the image loaded" and "the
image 404'd", so the only fix that holds is to check before using, and to send
nothing when nothing checks out.

The provider routes are pinned too: a new action shipped without a picture source
would quietly go back to being a card with a blank space in the middle.
"""
import unittest

from cogs import social as social_module
from utils import gifs


def _image(status=200, content_type="image/gif"):
    return {"status": status, "content_type": content_type, "payload": None}


def _json(payload, status=200):
    return {"status": status, "content_type": "application/json", "payload": payload}


class _Content:
    async def read(self, _n=None):
        return b"GIF89a"


class _Response:
    def __init__(self, spec):
        self.status = spec.get("status", 200)
        self.headers = {"Content-Type": spec.get("content_type", "application/json")}
        self._payload = spec.get("payload")
        self.content = _Content()
        self.released = False

    async def json(self, *args, **kwargs):
        return self._payload

    def release(self):
        self.released = True


class _Session:
    """A fake aiohttp session: a URL-to-response table that counts its calls."""

    def __init__(self, routes):
        self.routes = {url: list(specs) for url, specs in routes.items()}
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        specs = self.routes.get(url)
        if not specs:
            return _Response({"status": 404, "content_type": "text/html"})
        spec = specs[0] if len(specs) == 1 else specs.pop(0)
        return _Response(spec)

    def hits(self, url):
        return sum(1 for call in self.calls if call == url)


PURRBOT = "https://api.purrbot.site/v2/img/sfw/poke/gif"
OTAKU = "https://api.otakugifs.xyz/gif?reaction=poke"
GOOD_GIF = "https://cdn.purrbot.site/sfw/poke/gif/poke_001.gif"


class TheRoutes(unittest.TestCase):
    def setUp(self):
        gifs.clear_caches()

    def test_a_new_action_cannot_ship_without_a_picture_source(self):
        for action in social_module.ACTIONS_DATA:
            self.assertIn(action, gifs.ROUTES, f"/action {action} has no picture source")

    def test_every_bond_and_ceremony_has_a_picture_source(self):
        """A proposal card with the wrong picture is a bug report; a proposal
        card with no picture source is the same report waiting to happen."""
        needed = {"wedding"}
        for kind, spec in social_module.BOND_KINDS.items():
            needed.add(spec["gif"])
        for action in sorted(needed):
            self.assertTrue(
                gifs.candidates(action), f"bond/proposal gif {action!r} has no route")
        # The marriage ring is a fixed asset, not a fetched reaction.
        self.assertTrue(gifs.NOTO_GIFS["ring"].startswith(gifs.NOTO_GIF_BASE))

    def test_the_hand_typed_urls_are_gone(self):
        """They were all 404, and a link left in the cog is a link somebody
        reuses. The words stay; the URLs do not."""
        for action, data in social_module.ACTIONS_DATA.items():
            self.assertNotIn("gifs", data, f"action {action} still carries URLs")

    def test_a_bonk_borrows_a_slap_because_no_provider_draws_one(self):
        self.assertTrue(gifs.candidates("bonk"))
        self.assertNotIn("bonk", [reaction for _provider, reaction in gifs.candidates("bonk")])

    def test_an_unknown_action_has_no_route(self):
        self.assertEqual(gifs.candidates("banana"), ())

    def test_a_payload_is_only_read_where_a_url_could_be(self):
        self.assertEqual(gifs.pick_url("purrbot", {"link": GOOD_GIF}), GOOD_GIF)
        self.assertIsNone(gifs.pick_url("purrbot", {}))
        self.assertIsNone(gifs.pick_url("purrbot", {"link": "not-a-url"}))
        self.assertIsNone(gifs.pick_url("purrbot", None))
        self.assertIsNone(gifs.pick_url("nobody", {"link": GOOD_GIF}))
        self.assertIsNone(gifs.request_url("nobody", "poke"))


class ResolvingAnAction(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        gifs.clear_caches()

    async def test_a_working_provider_is_used(self):
        session = _Session({PURRBOT: [_json({"link": GOOD_GIF})], GOOD_GIF: [_image()]})
        self.assertEqual(await gifs.action_gif("poke", session=session), GOOD_GIF)

    async def test_a_provider_that_answers_garbage_falls_through(self):
        session = _Session({
            PURRBOT: [_json({"unexpected": "shape"})],
            OTAKU: [_json({"url": "https://cdn.otakugifs.xyz/gifs/poke/x.gif"})],
            "https://cdn.otakugifs.xyz/gifs/poke/x.gif": [_image()],
        })
        self.assertEqual(
            await gifs.action_gif("poke", session=session),
            "https://cdn.otakugifs.xyz/gifs/poke/x.gif",
        )

    async def test_a_picture_that_does_not_exist_is_not_sent(self):
        """The exact failure that started this: the API answers, the file is 404,
        and the card must not be handed the dead link."""
        dead = "https://cdn.purrbot.site/sfw/poke/gif/gone.gif"
        live = "https://cdn.otakugifs.xyz/gifs/poke/live.gif"
        session = _Session({
            PURRBOT: [_json({"link": dead})],
            dead: [_image(status=404, content_type="text/html")],
            OTAKU: [_json({"url": live})],
            live: [_image()],
        })
        self.assertEqual(await gifs.action_gif("poke", session=session), live)

    async def test_nothing_verifiable_means_no_picture_at_all(self):
        session = _Session({})  # everything 404s
        self.assertIsNone(await gifs.action_gif("poke", session=session))
        self.assertIsNone(await gifs.action_gif("banana", session=session))

    async def test_a_non_image_answer_is_not_a_picture(self):
        """A "200 OK" that is an HTML error page is not an image either."""
        session = _Session({
            PURRBOT: [_json({"link": GOOD_GIF})],
            GOOD_GIF: [_image(content_type="text/html")],
            OTAKU: [],
        })
        self.assertIsNone(await gifs.action_gif("poke", session=session))

    async def test_a_provider_that_explodes_does_not_escape(self):
        class Exploding:
            async def get(self, *args, **kwargs):
                raise RuntimeError("connection reset")

        self.assertIsNone(await gifs.action_gif("poke", session=Exploding()))

    async def test_a_verified_url_is_remembered(self):
        session = _Session({PURRBOT: [_json({"link": GOOD_GIF})], GOOD_GIF: [_image()]})
        self.assertEqual(await gifs.action_gif("poke", session=session), GOOD_GIF)
        self.assertEqual(await gifs.action_gif("poke", session=session), GOOD_GIF)
        self.assertEqual(session.hits(GOOD_GIF), 1, "a confirmed url is reused")

    async def test_a_provider_that_just_failed_is_not_hit_again_in_the_cooldown(self):
        session = _Session({PURRBOT: [_json({"link": GOOD_GIF})], GOOD_GIF: [_image(status=500)]})
        await gifs.action_gif("poke", session=session)
        before = session.hits(PURRBOT)
        # A different action routed through the same provider: it must not pay the
        # same timeout again while the failure is fresh.
        await gifs.action_gif("hug", session=session)
        self.assertEqual(session.hits(PURRBOT), before, "a dead provider was retried")


class ResolvingAPetPicture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        gifs.clear_caches()

    async def test_the_cat_is_an_animated_gif_from_a_direct_endpoint(self):
        session = _Session({"https://cataas.com/cat/gif": [_image()]})
        self.assertEqual(await gifs.pet_picture("cat", session=session), "https://cataas.com/cat/gif")

    async def test_the_dog_comes_out_of_a_json_answer(self):
        dog = "https://images.dog.ceo/breeds/pug/n1.jpg"
        session = _Session({
            "https://dog.ceo/api/breeds/image/random": [_json({"message": dog})],
            dog: [_image(content_type="image/jpeg")],
        })
        self.assertEqual(await gifs.pet_picture("dog", session=session), dog)

    async def test_the_neutral_flavour_draws_no_animal(self):
        session = _Session({})
        self.assertIsNone(await gifs.pet_picture("neutral", session=session))
        self.assertIsNone(await gifs.pet_picture(None, session=session))

    async def test_a_cat_that_cannot_be_confirmed_is_no_cat(self):
        """Better no picture than a broken one, even for the default flavour."""
        session = _Session({
            "https://cataas.com/cat/gif": [_image(status=503, content_type="text/html")],
            "https://cataas.com/cat": [_image(status=503, content_type="text/html")],
        })
        self.assertIsNone(await gifs.pet_picture("cat", session=session))


if __name__ == "__main__":
    unittest.main()
