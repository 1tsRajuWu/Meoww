"""The bot's own name and picture, per server — and the promise it stays there.

Discord keeps ``nick``, ``avatar``, ``banner`` and ``bio`` on the bot's **member**
in one guild, which makes this the only way a server can put its own name and
icon on the bot. The failure this module exists to prevent is the one the global
profile already had: a server owner reaching something that changes the bot for
everybody. So the tests are about *containment* — which server a change lands on,
what is left alone, and what is refused before Discord is asked at all.
"""
import asyncio
import io
import os
import struct
import unittest
import zlib
from types import SimpleNamespace
from unittest import mock

from PIL import Image

import keep_alive
from tests.support import TempDatabaseMixin
from tests.test_dashboard import CSRF, GUILD_ID, _DashboardFixture, make_bot, page_href
from utils import db, identity

#: The second server ``make_bot()`` builds, which the bot is also in.
OTHER_GUILD = 999002


class _Member:
    """The bot's own member in a server: records exactly what it was told.

    Discord's own object is the only thing that can apply this, so the assertion
    that matters is not "the endpoint returned 200" but "these were the fields
    sent, and no others".
    """

    def __init__(self):
        self.calls: list[dict] = []

    async def edit(self, **fields):
        self.calls.append(fields)


class Validation(unittest.TestCase):
    """What may be sent at all, decided before anything is asked of Discord."""

    def test_the_four_formats_discord_takes(self):
        for data in (
            b"\x89PNG\r\n\x1a\n" + b"0" * 20,
            b"\xff\xd8\xff\xe0" + b"0" * 20,
            b"GIF89a" + b"0" * 20,
            b"RIFF\x00\x00\x00\x00WEBP" + b"0" * 20,
        ):
            self.assertIsNone(identity.image_problem(data), data[:6])

    def test_an_empty_file_says_so(self):
        self.assertIn("empty", identity.image_problem(b""))

    def test_something_that_is_not_an_image_is_named(self):
        self.assertIn("PNG", identity.image_problem(b"just some text"))

    def test_an_image_past_the_limit_says_how_big_it_was(self):
        problem = identity.image_problem(b"\x89PNG\r\n\x1a\n" + b"0" * (identity.MAX_IMAGE_BYTES + 1))
        self.assertIn("MB", problem)

    def test_a_nickname_past_discords_limit_is_refused(self):
        self.assertIsNone(identity.name_problem("x" * identity.MAX_NICK))
        self.assertIn(str(identity.MAX_NICK), identity.name_problem("x" * (identity.MAX_NICK + 1)))

    def test_a_bio_past_discords_limit_is_refused(self):
        self.assertIsNone(identity.bio_problem("x" * identity.MAX_BIO))
        self.assertIsNotNone(identity.bio_problem("x" * (identity.MAX_BIO + 1)))

    def test_no_value_at_all_is_not_a_problem(self):
        self.assertIsNone(identity.name_problem(None))
        self.assertIsNone(identity.bio_problem(None))


def _png_that_declares(width: int, height: int) -> bytes:
    """A real one-pixel PNG whose header has been rewritten to say it is enormous.

    The IHDR checksum is fixed to match, so the file identifies as a PNG of that
    size while carrying no such picture — which is exactly the shape a
    decompression bomb has, minus the bytes. Enough for the guard, which reads
    the header and nothing else, and a few dozen bytes rather than the hundreds
    of megabytes a real picture of this size would cost to build here.
    """
    out = io.BytesIO()
    Image.new("L", (1, 1), 0).save(out, format="PNG")
    data = bytearray(out.getvalue())
    self_check = bytes(data[12:16])
    assert self_check == b"IHDR", self_check
    data[16:24] = struct.pack(">II", width, height)
    data[29:33] = struct.pack(">I", zlib.crc32(b"IHDR" + bytes(data[16:29])) & 0xFFFFFFFF)
    return bytes(data)


class PicturesThatWouldCostTooMuch(unittest.TestCase):
    """A file's size is not the memory it costs to look at.

    Four megabytes is the bound on the *file*, and a PNG is compressed: one flat
    colour four megabytes long can declare a bitmap of thousands of megapixels,
    and ``Image.load()`` allocates all of it before ``thumbnail`` ever runs. The
    header is what knows, so the header is read and the pixels never are.
    """

    def test_a_picture_past_the_ceiling_is_refused_with_its_own_size(self):
        problem = identity.image_problem(_png_that_declares(9_000, 9_000))
        self.assertIsNotNone(problem)
        self.assertIn("pixels", problem)
        self.assertIn("81 million", problem)
        self.assertIn(f"{identity.MAX_IMAGE_PIXELS // 1_000_000} million", problem)

    def test_a_picture_inside_the_ceiling_is_still_taken(self):
        out = io.BytesIO()
        Image.new("RGB", (32, 32), "red").save(out, format="PNG")
        self.assertIsNone(identity.image_problem(out.getvalue()))

    def test_taming_a_picture_past_the_ceiling_never_decodes_it(self):
        over = _png_that_declares(9_000, 9_000)
        with mock.patch.object(Image.Image, "load", side_effect=AssertionError("decoded")):
            self.assertEqual(identity.tame_image(over, max_edge=512), over)

    def test_the_ceiling_takes_a_phone_photograph(self):
        """4032×3024 is the picture this bound must never be the reason for."""
        self.assertLess(4032 * 3024, identity.MAX_IMAGE_PIXELS)


class Applying(unittest.IsolatedAsyncioTestCase):
    """``apply`` mentions only what changed — that is what keeps a form safe."""

    async def test_only_the_fields_it_was_given_are_sent(self):
        member = _Member()
        await identity.apply(member, nick="Meow")
        self.assertEqual(member.calls, [{"nick": "Meow"}])

    async def test_an_untouched_image_is_never_mentioned(self):
        """A nickname change must not clear a picture as a side effect."""
        member = _Member()
        await identity.apply(member, nick="Meow", bio="hi")
        self.assertNotIn("avatar", member.calls[0])
        self.assertNotIn("banner", member.calls[0])

    async def test_none_clears_a_field(self):
        member = _Member()
        await identity.apply(member, avatar=None, banner=None)
        self.assertEqual(member.calls, [{"avatar": None, "banner": None}])

    async def test_nothing_to_change_asks_discord_nothing(self):
        member = _Member()
        await identity.apply(member)
        self.assertEqual(member.calls, [])

    async def test_a_missing_member_is_not_an_error_here(self):
        """The caller decides what a cold cache means; this must not raise."""
        await identity.apply(None, nick="Meow")


class Storing(TempDatabaseMixin, unittest.TestCase):
    """Two servers, two profiles — the whole point of the table."""

    def setUp(self):
        self.use_temp_database("identity.db")
        asyncio.run(db.setup())

    def tearDown(self):
        self.release_temp_database()

    def test_a_server_that_has_never_set_one_reads_empty(self):
        profile = identity.read(GUILD_ID)
        self.assertIsNone(profile["nick"])
        self.assertIsNone(profile["avatar"])

    def test_what_is_written_is_read_back(self):
        self.assertTrue(identity.write(GUILD_ID, nick="Meow", bio="hi", updated_by=7))
        profile = identity.read(GUILD_ID)
        self.assertEqual(profile["nick"], "Meow")
        self.assertEqual(profile["bio"], "hi")
        self.assertEqual(profile["updated_by"], 7)

    def test_writing_a_nickname_leaves_the_stored_picture_alone(self):
        identity.write(GUILD_ID, nick="a", bio=None, avatar=b"PNGDATA", banner=b"BANNER")
        identity.write(GUILD_ID, nick="b", bio=None)
        profile = identity.read(GUILD_ID)
        self.assertEqual(profile["nick"], "b")
        self.assertEqual(profile["avatar"], b"PNGDATA")
        self.assertEqual(profile["banner"], b"BANNER")

    def test_a_written_none_clears_the_picture(self):
        identity.write(GUILD_ID, nick=None, bio=None, avatar=b"PNGDATA")
        identity.write(GUILD_ID, nick=None, bio=None, avatar=None)
        self.assertIsNone(identity.read(GUILD_ID)["avatar"])

    def test_one_servers_profile_never_shows_up_in_another(self):
        identity.write(GUILD_ID, nick="First", bio=None)
        identity.write(OTHER_GUILD, nick="Second", bio=None)
        self.assertEqual(identity.read(GUILD_ID)["nick"], "First")
        self.assertEqual(identity.read(OTHER_GUILD)["nick"], "Second")

    def test_clearing_forgets_the_whole_profile(self):
        identity.write(GUILD_ID, nick="Meow", bio="hi")
        self.assertTrue(identity.clear(GUILD_ID))
        self.assertIsNone(identity.read(GUILD_ID)["nick"])


class TheEndpoint(_DashboardFixture):
    """Who may change it, and what actually reaches Discord."""

    def setUp(self):
        super().setUp()
        self.member = _Member()
        patcher = mock.patch.object(keep_alive, "_identity_member", return_value=self.member)
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, **body):
        return self.api("POST", "/api/identity", body)

    def multipart(self, **form):
        return self.client.post(
            "/api/identity",
            data=form,
            headers={"X-CSRF-Token": CSRF},
            content_type="multipart/form-data",
        )

    def test_it_needs_a_session(self):
        response = self.client.post("/api/identity", json={"guild_id": GUILD_ID, "nick": "x"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.member.calls, [])

    def test_it_needs_the_csrf_token(self):
        self.sign_in()
        response = self.client.post("/api/identity", json={"guild_id": GUILD_ID, "nick": "x"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.member.calls, [])

    def test_an_administrators_link_may_change_its_own_server(self):
        self.sign_in(scope="guild", guild_id=GUILD_ID)
        response = self.save(guild_id=GUILD_ID, nick="Server Bot", bio="Ask me anything")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.member.calls, [{"nick": "Server Bot", "bio": "Ask me anything"}]
        )
        self.assertEqual(identity.read(GUILD_ID)["nick"], "Server Bot")

    def test_a_link_for_one_server_cannot_reach_another(self):
        """A scoped session stops at its own server — and *nothing* is written.

        This used to answer 200 after quietly re-aiming the save at the session's
        own server, so a form that named another server renamed the bot in a
        different one and reported success. ``guild_id`` in the body is now
        checked against the session's own scope: a name that is not the session's
        is refused, in words that say which server was refused and why.
        """
        self.sign_in(scope="guild", guild_id=GUILD_ID)
        response = self.save(guild_id=OTHER_GUILD, nick="Not Yours")
        self.assertEqual(response.status_code, 403)
        self.assertIn("do not administer", response.get_json()["error"])
        self.assertEqual(identity.read(OTHER_GUILD)["nick"], None)
        self.assertEqual(identity.read(GUILD_ID)["nick"], None, "the save was re-aimed")

    def test_an_empty_nickname_asks_discord_to_remove_it(self):
        self.sign_in(scope="guild", guild_id=GUILD_ID)
        identity.write(GUILD_ID, nick="Meow", bio=None)
        self.assertEqual(self.save(guild_id=GUILD_ID, nick="").status_code, 200)
        self.assertEqual(self.member.calls, [{"nick": ""}])
        self.assertIsNone(identity.read(GUILD_ID)["nick"])

    def test_across_the_board_write_is_refused_before_discord_is_asked(self):
        self.sign_in(scope="guild", guild_id=GUILD_ID)
        response = self.save(guild_id=GUILD_ID, nick="x" * (identity.MAX_NICK + 1))
        self.assertEqual(response.status_code, 400)
        self.assertIn(str(identity.MAX_NICK), response.get_json()["error"])
        self.assertEqual(self.member.calls, [])

    def test_a_file_that_is_not_an_image_is_named_in_the_answer(self):
        self.sign_in(scope="guild", guild_id=GUILD_ID)
        response = self.multipart(
            guild_id=str(GUILD_ID), avatar=(io.BytesIO(b"definitely not a picture"), "x.png")
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("PNG", response.get_json()["error"])
        self.assertEqual(self.member.calls, [])

    def test_an_uploaded_picture_is_applied_and_served_back(self):
        self.sign_in(scope="guild", guild_id=GUILD_ID)
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 40
        response = self.multipart(
            guild_id=str(GUILD_ID), avatar=(io.BytesIO(png), "pic.png")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.member.calls, [{"avatar": png}])
        served = self.client.get(
            f"/api/identity/image/avatar?guild_id={GUILD_ID}",
            headers={"X-CSRF-Token": CSRF},
        )
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.data, png)

    def test_a_real_sized_picture_is_accepted(self):
        """A photograph, not a favicon.

        The app-level body cap was 16 KB and it is checked *before* any view runs,
        so this panel could only ever accept a file smaller than a favicon: a 20 KB
        PNG was answered with the 413 handler's sentence about *messages* ("That
        message is larger than the dashboard accepts (16 KB). Shorten it or split
        the send."), and a 48-byte one was applied. The only uploaded file in the
        suite was 46 bytes, which is why nothing caught it.
        """
        self.sign_in(scope="guild", guild_id=GUILD_ID)
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 60_000
        response = self.multipart(guild_id=str(GUILD_ID), avatar=(io.BytesIO(png), "pic.png"))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.member.calls, [{"avatar": png}])

    def test_a_picture_over_discords_limit_names_that_limit(self):
        """4 MB, in the validator's own words — not the generic body refusal."""
        self.sign_in(scope="guild", guild_id=GUILD_ID)
        oversized = b"\x89PNG\r\n\x1a\n" + b"0" * (identity.MAX_IMAGE_BYTES + 1)
        response = self.multipart(guild_id=str(GUILD_ID), avatar=(io.BytesIO(oversized), "big.png"))
        self.assertEqual(response.status_code, 400)
        self.assertIn(str(identity.MAX_IMAGE_BYTES // (1024 * 1024)), response.get_json()["error"])
        self.assertEqual(self.member.calls, [])

    def test_a_body_past_what_two_pictures_can_be_is_refused_as_an_upload(self):
        """The outer bound, and the sentence that names it."""
        self.sign_in(scope="guild", guild_id=GUILD_ID)
        huge = b"\x89PNG\r\n\x1a\n" + b"0" * (2 * identity.MAX_IMAGE_BYTES + 200_000)
        response = self.multipart(guild_id=str(GUILD_ID), avatar=(io.BytesIO(huge), "big.png"))
        self.assertEqual(response.status_code, 413)
        error = response.get_json()["error"]
        self.assertIn("upload", error)
        self.assertNotIn("message is larger", error)
        self.assertEqual(self.member.calls, [])

    def test_the_other_routes_keep_the_small_limit(self):
        """Widening the bound for pictures must not widen it anywhere else.

        Every other endpoint here takes a small JSON object, and the composer's
        ceiling is Discord's own 2000 characters — so a body past 16 KB is still
        refused, in the words that fit it.
        """
        self.sign_in(scope="guild", guild_id=GUILD_ID)
        response = self.client.post(
            "/api/send-message",
            json={"guild_id": GUILD_ID, "channel_ids": [7], "content": "x" * 30_000},
            headers={"X-CSRF-Token": CSRF},
        )
        self.assertEqual(response.status_code, 413)
        self.assertIn("16 KB", response.get_json()["error"])

    def test_nothing_to_change_is_refused_rather_than_answered_ok(self):
        self.sign_in(scope="guild", guild_id=GUILD_ID)
        response = self.save(guild_id=GUILD_ID)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Nothing to change", response.get_json()["error"])

    def test_a_discord_refusal_is_reported_and_recorded_nowhere(self):
        """A picture Discord refused must not end up in the panel as if it worked."""
        self.sign_in(scope="guild", guild_id=GUILD_ID)

        async def refuse(**fields):
            self.member.calls.append(fields)
            raise RuntimeError("Invalid Form Body")

        with mock.patch.object(self.member, "edit", refuse):
            response = self.save(guild_id=GUILD_ID, nick="Meow")
        self.assertEqual(response.status_code, 502)
        self.assertIsNone(identity.read(GUILD_ID)["nick"])

    def test_a_cleared_nickname_is_stored_as_nothing_not_as_an_empty_string(self):
        """``""`` is how Discord spells "no nickname"; the record should agree."""
        self.sign_in(scope="guild", guild_id=GUILD_ID)
        identity.write(GUILD_ID, nick="Meow", bio=None)
        self.save(guild_id=GUILD_ID, nick="")
        self.assertIsNone(identity.read(GUILD_ID)["nick"])

    def test_the_page_shows_this_servers_profile_and_not_its_neighbours(self):
        self.sign_in(scope="guild", guild_id=GUILD_ID)
        identity.write(GUILD_ID, nick="Here", bio=None)
        identity.write(OTHER_GUILD, nick="There", bio=None)
        # The profile form is its own page now; the assertion follows the form.
        page = self.client.get(f'{page_href("identity")}?guild_id={GUILD_ID}')
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn('value="Here"', body)
        # Matched as the rendered *value*, not as a bare word anywhere on the
        # page. The bare word was the first version of this line, and it failed
        # the moment an unrelated comment in `dashboard.html` used the English
        # word "There" — a test that goes red on prose teaches the next person to
        # delete the prose, and the thing it was guarding (this server's nickname
        # is the only one on the page) is exactly the value, rendered in one
        # place: the input on the identity card.
        self.assertNotIn('value="There"', body)

    def test_an_unreadable_member_does_not_break_the_page(self):
        self.sign_in(scope="guild", guild_id=GUILD_ID)
        with mock.patch.object(keep_alive, "_identity_member", return_value=None):
            page = self.client.get(f'{page_href("identity")}?guild_id={GUILD_ID}')
        self.assertEqual(page.status_code, 200)
        self.assertIn("This server's bot", page.get_data(as_text=True))


class ThePanelIsRendered(unittest.TestCase):
    """The controls exist, and they are the ones the endpoint accepts."""

    def setUp(self):
        self.bot, self.guild, self.other = make_bot()
        self.bot.tree = SimpleNamespace(get_commands=lambda: [])
        patcher = mock.patch.object(keep_alive, "BOT_INSTANCE", self.bot)
        patcher.start()
        self.addCleanup(patcher.stop)
        client = keep_alive.app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True
            session["csrf_token"] = CSRF
            session["auth_scope"] = "guild"
            session["guild_id"] = GUILD_ID
        # Two pages, because the two panels are two pages: the profile form is the
        # Identity page and the command list is the Commands page, and a test that
        # asked one address for both would be reading a page that no longer exists.
        get = lambda page: client.get(  # noqa: E731 - one line, used twice
            f'{page_href(page)}?guild_id={GUILD_ID}'
        ).get_data(as_text=True)
        self.page = get("identity")
        self.commands_page = get("commands")

    def test_the_controls_are_on_the_page(self):
        for element in (
            'id="identity-nick"', 'id="identity-bio"',
            'id="identity-avatar-file"', 'id="identity-banner-file"',
            'id="identity-save"', 'id="identity-clear"',
        ):
            self.assertIn(element, self.page)

    def test_the_form_posts_multipart_to_the_identity_route(self):
        """A real form, to the endpoint that owns the write — not a fetch.

        The panel used to POST through JavaScript; the page carries a
        `multipart/form-data` form now, which is why the controls above are named
        for the endpoint's own fields and why this reads the form rather than a
        call site.
        """
        self.assertIn('action="/api/identity"', self.page)
        self.assertIn('enctype="multipart/form-data"', self.page)
        self.assertIn('method="post"', self.page)

    def test_the_command_panel_explains_why_the_list_is_per_server(self):
        self.assertIn("Commands in this server", self.commands_page)
        self.assertIn("not in this server's picker at all", self.commands_page)


def _gradient(size: tuple[int, int], fmt: str = "JPEG") -> bytes:
    """A photograph-shaped picture: smooth, so it compresses like one."""
    image = Image.linear_gradient("L").resize(size).convert("RGB")
    out = io.BytesIO()
    image.save(out, format=fmt)
    return out.getvalue()


def _noisy_png(size: tuple[int, int]) -> bytes:
    """The other extreme: entropy, which PNG cannot squeeze at all."""
    image = Image.frombytes("RGBA", size, os.urandom(size[0] * size[1] * 4))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class WhatAPictureIsKeptAt(unittest.TestCase):
    """A stored picture is shrunk once, and the small one is what is kept.

    These bytes are counted twice on the way out of this process — once to
    Discord inside the member edit, once to Supabase inside the ``bot.db`` that
    is uploaded after every change and snapshotted hourly — and the picture is
    the part of that file gzip cannot help with, because a JPEG is already
    compressed. So an unbounded upload makes every future backup bigger for
    good, and this is where the bound is applied.
    """

    def test_a_photograph_past_the_edge_comes_back_smaller_and_inside_it(self):
        original = _gradient((1200, 1200))
        tamed = identity.tame_image(original, max_edge=identity.AVATAR_MAX_EDGE)
        self.assertLess(len(tamed), len(original) / 2, "it was not actually shrunk")
        with Image.open(io.BytesIO(tamed)) as picture:
            self.assertLessEqual(max(picture.size), identity.AVATAR_MAX_EDGE)

    def test_a_banner_keeps_more_of_its_width_than_an_avatar(self):
        original = _gradient((2048, 1024))
        tamed = identity.tame_image(original, max_edge=identity.BANNER_MAX_EDGE)
        with Image.open(io.BytesIO(tamed)) as picture:
            self.assertEqual(max(picture.size), identity.BANNER_MAX_EDGE)
            self.assertGreater(picture.size[0], picture.size[1], "the wide shape was lost")

    def test_bytes_that_are_not_a_picture_come_back_untouched(self):
        """The validator above is a magic-byte check, so this is a real arrival.

        A file that says PNG and decodes to nothing must be stored as it was:
        guessing at it would store a picture the server never chose.
        """
        payload = b"\x89PNG\r\n\x1a\n" + b"0" * 40
        self.assertIs(identity.tame_image(payload, max_edge=identity.AVATAR_MAX_EDGE), payload)

    def test_a_picture_that_is_already_small_is_not_re_encoded(self):
        payload = _gradient((64, 64))
        self.assertLess(len(payload), identity.ALREADY_SMALL_BYTES)
        self.assertIs(identity.tame_image(payload, max_edge=identity.AVATAR_MAX_EDGE), payload)

    def test_an_animation_is_not_flattened_to_its_first_frame(self):
        frames = [Image.new("P", (64, 64), 1), Image.new("P", (64, 64), 2)]
        out = io.BytesIO()
        frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=100)
        payload = out.getvalue()
        self.assertIs(identity.tame_image(payload, max_edge=identity.AVATAR_MAX_EDGE), payload)

    def test_transparency_survives_the_re_encode(self):
        original = _noisy_png((600, 600))
        tamed = identity.tame_image(original, max_edge=identity.AVATAR_MAX_EDGE)
        self.assertLessEqual(len(tamed), len(original))
        with Image.open(io.BytesIO(tamed)) as picture:
            self.assertIn("A", picture.mode, "the transparency was replaced with a background")
            self.assertLessEqual(max(picture.size), identity.AVATAR_MAX_EDGE)

    def test_shrinking_an_already_shrunk_picture_is_a_no_op(self):
        """The route tames, and ``write`` tames: that must not cost quality twice."""
        once = identity.tame_image(_gradient((1200, 1200)), max_edge=identity.AVATAR_MAX_EDGE)
        self.assertEqual(
            identity.tame_image(once, max_edge=identity.AVATAR_MAX_EDGE), once
        )

    def test_the_record_is_the_bound_however_it_is_written(self):
        """``write`` is the last door before the file every backup carries."""
        identity.write(GUILD_ID, nick=None, bio=None, avatar=_gradient((1200, 1200)))
        stored = identity.read(GUILD_ID)["avatar"]
        with Image.open(io.BytesIO(stored)) as picture:
            self.assertLessEqual(max(picture.size), identity.AVATAR_MAX_EDGE)


class TheUploadedPictureIsSentSmall(TheEndpoint):
    """What Discord is given, and what is kept, are the same small picture."""

    def test_discord_and_the_record_both_get_the_shrunk_picture(self):
        self.sign_in(scope="guild", guild_id=GUILD_ID)
        original = _gradient((1200, 1200))
        response = self.multipart(
            guild_id=str(GUILD_ID), avatar=(io.BytesIO(original), "pic.jpg")
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        applied = self.member.calls[0]["avatar"]
        self.assertLess(len(applied), len(original) / 2)
        with Image.open(io.BytesIO(applied)) as picture:
            self.assertLessEqual(max(picture.size), identity.AVATAR_MAX_EDGE)
        self.assertEqual(identity.read(GUILD_ID)["avatar"], applied, "two different pictures")

    def test_a_picture_it_cannot_improve_is_applied_exactly_as_uploaded(self):
        self.sign_in(scope="guild", guild_id=GUILD_ID)
        payload = b"\x89PNG\r\n\x1a\n" + b"0" * 40
        self.assertEqual(
            self.multipart(guild_id=str(GUILD_ID), avatar=(io.BytesIO(payload), "p.png")).status_code,
            200,
        )
        self.assertEqual(self.member.calls, [{"avatar": payload}])

    def test_discord_is_still_told_nothing_about_a_picture_it_was_not_given(self):
        """The shrink lives in one branch, and it must not have widened the form."""
        self.sign_in(scope="guild", guild_id=GUILD_ID)
        self.assertEqual(self.save(guild_id=GUILD_ID, nick="Meow").status_code, 200)
        self.assertEqual(self.member.calls, [{"nick": "Meow"}])


if __name__ == "__main__":
    unittest.main()
