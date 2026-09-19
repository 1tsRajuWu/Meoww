"""The dashboard composer: the bot posts what an administrator typed.

This is the most powerful surface in the project — it speaks as the bot, in any
channel, tagging anyone — so the tests are about what it *refuses*: a request
with no session, no CSRF token, a tag that is not a member or role of the guild
being written to, a channel the bot cannot send in, and a guild the signed-in
session was not pinned to. Everything the browser sends is re-resolved against
the live guild, because a browser can send anything.
"""
import os
import re
import unittest
from types import SimpleNamespace
from unittest import mock

import discord

GUILD_ID = 777001
OTHER_GUILD_ID = 777002
CHANNEL_ID = 555001
QUIET_CHANNEL_ID = 555002
OTHER_CHANNEL_ID = 555003
ROLE_ID = 444001
MEMBER_ID = 333001
CSRF = "csrf-token"


class _Role:
    def __init__(self, rid, name, default=False):
        self.id = rid
        self.name = name
        self._default = default
        self.mention = f"<@&{rid}>"

    def is_default(self):
        return self._default


class _Member:
    def __init__(self, uid, name, display=None):
        self.id = uid
        self.name = name
        self.display_name = display or name
        self.mention = f"<@{uid}>"


class _BaseChannel:
    """What a text channel and a voice channel really have in common.

    ``is_news`` is deliberately *not* here: ``discord.TextChannel`` has it and
    ``discord.VoiceChannel`` does not, so a voice stand-in that inherited it would
    be inventing an attribute the guard test below exists to catch.
    """

    def __init__(self, cid, name, *, can_send=True, viewable=True, category=None):
        self.id = cid
        self.name = name
        self.category = SimpleNamespace(name=category) if category else None
        self.slowmode_delay = 0
        self.sent = []
        self.send_error = None
        self._can_send = can_send
        self._viewable = viewable

    def permissions_for(self, member):
        return SimpleNamespace(view_channel=self._viewable, send_messages=self._can_send)

    async def send(self, content=None, embed=None, allowed_mentions=None, view=None, **kwargs):
        if self.send_error is not None:
            raise self.send_error
        # ``view`` is recorded because the shared card helper (``utils.style.send_card``)
        # upgrades an embed to a Components V2 layout, and a stand-in that dropped it
        # would show an empty message where production posts a card.
        self.sent.append({
            "content": content,
            "embed": embed,
            "allowed_mentions": allowed_mentions,
            "view": view,
        })
        return SimpleNamespace(
            id=len(self.sent), jump_url=f"https://discord.com/channels/{GUILD_ID}/{self.id}/{len(self.sent)}"
        )


class _Channel(_BaseChannel):
    """A text channel — an announcement channel is one of these with news on."""

    def __init__(self, cid, name, *, news=False, **kwargs):
        super().__init__(cid, name, **kwargs)
        self._news = news
        # The real discriminator, from the real enum: production reads
        # ``channel.type`` to tell a voice channel from a text one.
        self.type = discord.ChannelType.news if news else discord.ChannelType.text

    def is_news(self):
        return self._news


class _VoiceChannel(_BaseChannel):
    """A voice channel, which is Messageable and takes a message in its chat."""

    def __init__(self, cid, name, **kwargs):
        super().__init__(cid, name, **kwargs)
        self.type = discord.ChannelType.voice


class _Guild:
    def __init__(self, gid, name, channels, *, roles=(), members=(), voice_channels=()):
        self.id = gid
        self.name = name
        # discord.py has no ``Guild.news_channels``: an announcement channel is a
        # TextChannel whose type is news, so it comes back from ``text_channels``
        # with every other one. Splitting the list in two here is what let a live
        # AttributeError sit behind a green test suite. ``voice_channels`` *is* a
        # real attribute, and the picker reads it.
        self.text_channels = list(channels)
        self.voice_channels = list(voice_channels)
        self.roles = [_Role(gid, "@everyone", default=True)] + list(roles)
        self.members = list(members)
        self.member_count = len(self.members)  # what the server says it has
        self.fetchable = {}  # ids Discord would hand back on a lookup
        self.me = SimpleNamespace(id=999)
        # Every channel the gateway can resolve, not just the text ones: a
        # lookup map that cannot find a voice channel would fail a send for a
        # reason production does not have.
        self._channels = {c.id: c for c in list(channels) + list(voice_channels)}

    async def fetch_member(self, uid):
        """The one-id lookup, which works even with the Members intent off."""
        member = self.fetchable.get(uid)
        if member is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Unknown Member"), "unknown")
        return member

    def get_channel(self, cid):
        return self._channels.get(cid)

    def get_thread(self, cid):
        return None

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)


class _Bot:
    def __init__(self, guilds):
        self._guilds = {g.id: g for g in guilds}
        self.guilds = list(guilds)
        self._has_started = True
        self.user = SimpleNamespace(id=999)
        self.loop = None  # no loop -> the runner falls back to asyncio.run

    def get_guild(self, gid):
        return self._guilds.get(gid)


class _ComposerFixture(unittest.TestCase):
    """The composer's gateway stand-ins and client, and **no tests of its own**.

    Split out because a class that is both the fixture and the assertions makes
    every other module that inherits it re-run these tests: ``pytest`` collects a
    ``unittest.TestCase`` subclass wherever it appears, including imported, and an
    inherited method is re-collected in each subclass. ``tests/tests_scheduler.py``
    inherits it in five classes (``SchedulerCase`` and its four subclasses), so the
    composer's 39 tests ran six times over — 195 extra executions, each opening a
    throwaway database, every one of them counted as a pass. A fixture with no test
    methods of its own contributes nothing wherever it is inherited or imported.
    """

    def setUp(self):
        self.standard = _Channel(CHANNEL_ID, "announcements", category="News")
        self.quiet = _Channel(QUIET_CHANNEL_ID, "staff-only", can_send=False)
        self.hidden = _Channel(OTHER_CHANNEL_ID, "secret", viewable=False)
        self.news = _Channel(555004, "updates", news=True)
        self.voice = _VoiceChannel(555005, "General Voice", category="Voice")
        self.hidden_voice = _VoiceChannel(555006, "Staff Call", viewable=False)
        self.muted_voice = _VoiceChannel(555007, "Muted Call", can_send=False)
        self.role = _Role(ROLE_ID, "Members")
        self.member = _Member(MEMBER_ID, "raj", "RAJ")
        # Two more people, so "the whole server" is a claim with something to prove.
        self.others = [
            _Member(MEMBER_ID + 1, "ash", "Ash"),
            _Member(MEMBER_ID + 2, "zed", "Zed"),
        ]
        self.guild = _Guild(
            GUILD_ID,
            "Meow Server",
            [self.standard, self.quiet, self.hidden, self.news],
            roles=[self.role],
            members=[self.member, *self.others],
            voice_channels=[self.voice, self.hidden_voice, self.muted_voice],
        )
        self.other_guild = _Guild(
            OTHER_GUILD_ID, "Other Server", [_Channel(OTHER_CHANNEL_ID + 50, "general")]
        )

        import keep_alive

        self.keep_alive = keep_alive
        # The flood bucket is process-wide and keyed by client address, so one
        # test's sends would otherwise rate-limit the next one.
        keep_alive._send_attempts.clear()
        self._patched = mock.patch.object(keep_alive, "BOT_INSTANCE", _Bot([self.guild, self.other_guild]))
        self._patched.start()
        self.client = keep_alive.app.test_client()
        self._session(guild_id=GUILD_ID, scope="guild")

    def tearDown(self):
        self._patched.stop()

    def _session(self, *, guild_id=GUILD_ID, scope="guild"):
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["csrf_token"] = CSRF
            session["auth_scope"] = scope
            session["guild_id"] = guild_id

    def _get(self, url):
        return self.client.get(url, headers={"X-CSRF-Token": CSRF})

    def _post(self, url, body, *, csrf=CSRF):
        headers = {"X-CSRF-Token": csrf} if csrf else {}
        return self.client.post(url, json=body, headers=headers)

    def _send(self, **payload):
        body = {
            "guild_id": GUILD_ID,
            "channel_ids": [CHANNEL_ID],
            "content": "hello",
            "mentions": [],
        }
        body.update(payload)
        return self._post("/api/send-message", body)


class ComposerCase(_ComposerFixture):
    """The composer's own tests, driven through that stand-in gateway."""

    # ── access ───────────────────────────────────────────────────────────
    def test_every_route_requires_a_session(self):
        with self.client.session_transaction() as session:
            session.clear()
        for url in ("/api/channels", "/api/mentionables"):
            self.assertEqual(self._get(url).status_code, 401, url)
        self.assertEqual(self._post("/api/send-message", {}).status_code, 401)

    def test_every_route_requires_the_csrf_token(self):
        self.assertEqual(self._post("/api/send-message", {}, csrf=None).status_code, 403)
        self.assertEqual(self._post("/api/send-message", {}, csrf="wrong").status_code, 403)

    def test_a_pinned_session_cannot_be_redirected_to_another_guild(self):
        """The body asks for the other server; the session is pinned to this one."""
        resp = self._send(guild_id=OTHER_GUILD_ID, channel_ids=[OTHER_CHANNEL_ID + 50])
        self.assertFalse(resp.get_json()["success"])
        for channel in self.other_guild.text_channels:
            self.assertEqual(channel.sent, [], "the other guild must not be spoken to")

    # ── the channel picker ───────────────────────────────────────────────
    def test_channels_lists_sendable_ones_and_hides_invisible_ones(self):
        data = self._get(f"/api/channels?guild_id={GUILD_ID}").get_json()
        self.assertTrue(data["success"])
        by_id = {c["id"]: c for c in data["channels"]}
        self.assertNotIn(str(self.hidden.id), by_id, "a channel the bot cannot see is not offered")
        self.assertTrue(by_id[str(self.standard.id)]["can_send"])
        self.assertFalse(by_id[str(self.quiet.id)]["can_send"], "no permission is shown, not hidden")
        self.assertIn(str(self.news.id), by_id, "announcement channels take messages too")
        self.assertEqual(by_id[str(self.standard.id)]["category"], "News")

    # ── voice channels ───────────────────────────────────────────────────
    def test_voice_channels_are_offered_and_marked_as_voice(self):
        """A voice channel has a text chat, and it is where the people in the call
        are reading — leaving it out made the one channel people sit in the one
        channel a message could not reach."""
        data = self._get(f"/api/channels?guild_id={GUILD_ID}").get_json()
        by_id = {c["id"]: c for c in data["channels"]}
        self.assertIn(str(self.voice.id), by_id, "a voice channel is somewhere a message can land")
        self.assertEqual(by_id[str(self.voice.id)]["kind"], "voice", "and it says so")
        self.assertEqual(by_id[str(self.standard.id)]["kind"], "text")
        self.assertEqual(by_id[str(self.voice.id)]["category"], "Voice")
        self.assertTrue(by_id[str(self.voice.id)]["can_send"])

    def test_voice_channels_follow_the_same_permission_rules(self):
        data = self._get(f"/api/channels?guild_id={GUILD_ID}").get_json()
        by_id = {c["id"]: c for c in data["channels"]}
        self.assertNotIn(str(self.hidden_voice.id), by_id, "one the bot cannot see is not offered")
        self.assertFalse(by_id[str(self.muted_voice.id)]["can_send"], "no permission is shown, not hidden")

    def test_a_message_can_land_in_a_voice_channel(self):
        response = self._send(channel_ids=[self.voice.id], content="reading this in the call").get_json()
        self.assertTrue(response["success"], response)
        self.assertEqual(self.voice.sent[-1]["content"], "reading this in the call")
        self.assertEqual(response["sent"][0]["name"], "General Voice")

    def test_a_voice_channel_with_its_text_chat_off_says_so(self):
        """No field in the API reports whether that chat is on, so the refusal is
        the first thing that knows — and "missing permission" would send an
        administrator hunting for a role overwrite that is not the problem."""
        self.voice.send_error = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            "Cannot send messages in a non-text channel",
        )
        response = self._send(channel_ids=[self.voice.id], content="hi").get_json()
        self.assertFalse(response["success"])
        error = response["failed"][0]["error"]
        self.assertIn("text chat", error, error)
        self.assertIn("turned off", error, error)
        self.assertNotIn("missing permission", error, "that sends them looking for the wrong thing")

    def test_ids_cross_to_the_browser_as_strings_a_snowflake_cannot_be_rounded(self):
        """A Discord id does not fit in a JavaScript number.

        ``1548437081120833657`` comes back from ``JSON.parse`` as
        ``1548437081120833536``. While these were sent as numbers, the picker
        filled in and then every send answered "That channel no longer exists":
        the id it posted back matched no channel at all.
        """
        live_sized = 1548437081120833657  # the real id of one of the server's channels
        self.assertGreater(live_sized, 2 ** 53 - 1, "the premise: real ids overflow a double")
        self.assertNotEqual(int(float(live_sized)), live_sized, "…and lose their last digits")

        channels = self._get(f"/api/channels?guild_id={GUILD_ID}").get_json()["channels"]
        self.assertTrue(channels)
        for ch in channels:
            self.assertIsInstance(ch["id"], str, ch)
            self.assertTrue(ch["id"].isdigit(), ch)

        mentions = self._get(f"/api/mentionables?guild_id={GUILD_ID}&q=mem").get_json()
        for item in mentions["users"] + mentions["roles"]:
            self.assertIsInstance(item["id"], str, item)
            self.assertTrue(item["id"].isdigit(), item)

    def test_a_channel_id_posted_back_as_a_string_still_sends(self):
        """What the browser now posts is a string, and it has to resolve."""
        response = self._send(channel_ids=[str(CHANNEL_ID)], content="string id")
        data = response.get_json()
        self.assertTrue(data["success"], data)
        self.assertEqual(self.standard.sent[-1]["content"], "string id")

    def test_the_stand_ins_only_expose_attributes_the_real_library_has(self):
        """A stand-in that invents an attribute hides the bug it exists to find.

        ``_Guild`` carried a ``news_channels`` list that discord.py does not have,
        so every test passed while the live ``/api/channels`` raised
        AttributeError and the picker sat empty behind a 500. Each stand-in may
        only expose names its real counterpart exposes, plus the two fields
        discord.py assigns in ``__init__`` instead of declaring on the class.
        """
        # Names a stand-in carries for the tests' own bookkeeping — where a faked
        # channel records what was sent, and what a faked gateway can look up —
        # rather than to imitate the API.
        bookkeeping = {"sent", "send_error", "fetchable"}
        cases = [
            (self.guild, discord.Guild),
            (self.standard, discord.TextChannel),
            (self.voice, discord.VoiceChannel),
            (self.role, discord.Role),
            (self.member, discord.Member),
        ]
        for stub, real in cases:
            with self.subTest(stub=type(stub).__name__, real=real.__name__):
                allowed = set(dir(real)) | {"id", "name"} | bookkeeping
                # Both the instance's own attributes and the ones its class
                # defines: an invented method hides a bug just as well as an
                # invented attribute.
                surfaced = set(vars(stub)) | set(vars(type(stub)))
                invented = {
                    name for name in surfaced
                    if not name.startswith("_") and name not in allowed
                }
                self.assertEqual(
                    invented, set(),
                    f"{type(stub).__name__} invents {sorted(invented)}, which "
                    f"{real.__name__} does not have",
                )

    # ── the tag picker ───────────────────────────────────────────────────
    def test_mentionables_finds_roles_and_members(self):
        data = self._get(f"/api/mentionables?guild_id={GUILD_ID}&q=raj").get_json()
        self.assertTrue(data["success"])
        self.assertEqual([u["id"] for u in data["users"]], [str(MEMBER_ID)])
        self.assertEqual([u["id"] for u in data["roles"]], [], "roles do not match 'raj'")

        roles = self._get(f"/api/mentionables?guild_id={GUILD_ID}&q=mem").get_json()["roles"]
        self.assertEqual([r["id"] for r in roles], [str(ROLE_ID)])

    def test_mentionables_offers_everyone_and_here_but_never_lists_everyone_as_a_role(self):
        data = self._get(f"/api/mentionables?guild_id={GUILD_ID}").get_json()
        self.assertEqual(sorted(s["id"] for s in data["special"]), ["@everyone", "@here"])
        self.assertNotIn(str(GUILD_ID), [r["id"] for r in data["roles"]], "@everyone is not a role")

    def test_everyone_stays_on_offer_while_it_is_being_typed(self):
        """A search that hides the thing you are typing toward is not a search.

        @everyone and @here were only offered once the query already contained
        the whole keyword, so typing "ev" showed nothing and the option looked
        missing.
        """
        # Each keyword is offered whenever what was typed is part of *it* — which
        # is how roles and members are matched too.
        expected = {
            "": ["@everyone", "@here"],
            "@": ["@everyone", "@here"],
            "e": ["@everyone", "@here"],
            "ev": ["@everyone"],
            "every": ["@everyone"],
            "everyone": ["@everyone"],
            "@everyone": ["@everyone"],
            "here": ["@here"],
            "raj": [],
        }
        for query, wanted in expected.items():
            data = self._get(f"/api/mentionables?guild_id={GUILD_ID}&q={query}").get_json()
            self.assertEqual(
                [s["id"] for s in data["special"]], wanted, f"typed {query!r}"
            )

    def test_an_empty_tag_box_lists_every_member(self):
        """Browsing the server is how you tag someone whose name you cannot spell."""
        data = self._get(f"/api/mentionables?guild_id={GUILD_ID}").get_json()
        self.assertEqual(
            [u["label"] for u in data["users"]],
            ["Ash", "RAJ", "Zed"],
            "every member, in a predictable order",
        )
        self.assertEqual(data["member_total"], 3)
        self.assertEqual(data["member_cache"], 3)
        for user in data["users"]:
            self.assertIsInstance(user["id"], str, "ids cross as strings")
            self.assertTrue(user["id"].isdigit(), user)

    def test_the_picker_admits_when_it_can_see_only_part_of_the_server(self):
        """With the Members intent off the cache is a fraction of the server."""
        with mock.patch.object(self.guild, "member_count", 40):
            data = self._get(f"/api/mentionables?guild_id={GUILD_ID}").get_json()
        self.assertEqual(data["member_cache"], 3, "what the bot can see")
        self.assertEqual(data["member_total"], 40, "what the server actually has")

    def test_a_capped_member_list_reports_what_it_left_out(self):
        """A cap nobody mentions is how a person you meant to tag stays invisible."""
        with mock.patch.object(self.keep_alive, "MENTION_MEMBER_LIST_MAX", 2):
            data = self._get(f"/api/mentionables?guild_id={GUILD_ID}").get_json()
        self.assertEqual([u["label"] for u in data["users"]], ["Ash", "RAJ"])
        self.assertEqual(data["member_total"], 3, "so the page can say what is not shown")

    def test_typing_searches_the_live_member_list_not_just_the_listed_page(self):
        """Any member is reachable, however short the listing was."""
        with mock.patch.object(self.keep_alive, "MENTION_MEMBER_LIST_MAX", 1):
            data = self._get(f"/api/mentionables?guild_id={GUILD_ID}&q=zed").get_json()
        self.assertEqual([u["label"] for u in data["users"]], ["Zed"])

    # ── sending ──────────────────────────────────────────────────────────
    def test_a_plain_message_is_posted_with_the_tags_in_the_content(self):
        resp = self._send(content="server meeting at 8", mentions=[str(MEMBER_ID), str(ROLE_ID)])
        data = resp.get_json()
        self.assertTrue(data["success"], data)
        self.assertEqual(len(self.standard.sent), 1)
        posted = self.standard.sent[0]
        # A ping only notifies from the content, so both tags must be there.
        self.assertIn(f"<@{MEMBER_ID}>", posted["content"])
        self.assertIn(f"<@&{ROLE_ID}>", posted["content"])
        self.assertIn("server meeting at 8", posted["content"])
        self.assertEqual([u.id for u in posted["allowed_mentions"].users], [MEMBER_ID])
        self.assertEqual([r.id for r in posted["allowed_mentions"].roles], [ROLE_ID])
        self.assertFalse(posted["allowed_mentions"].everyone, "a normal send must not ping everyone")

    def test_everyone_only_pings_when_it_was_asked_for(self):
        self._send(content="hi", mentions=[])
        self.assertFalse(self.standard.sent[-1]["allowed_mentions"].everyone)

        self._send(content="@everyone read this", mentions=["@everyone"])
        posted = self.standard.sent[-1]
        self.assertTrue(posted["allowed_mentions"].everyone)
        self.assertIn("@everyone", posted["content"])

    def test_here_is_posted_as_here_not_as_everyone(self):
        """The picker offers them side by side, so they must arrive separately."""
        self._send(content="server night", mentions=["@here"])
        posted = self.standard.sent[-1]
        self.assertIn("@here", posted["content"])
        self.assertNotIn("@everyone", posted["content"])
        self.assertTrue(posted["allowed_mentions"].everyone, "@here still needs it allowed")

        self._send(content="server night", mentions=["@everyone"])
        posted = self.standard.sent[-1]
        self.assertIn("@everyone", posted["content"])
        self.assertNotIn("@here", posted["content"])

    def test_a_title_without_an_embed_is_refused_rather_than_posted_empty(self):
        """Outside an embed the title is not sent, so a title alone is nothing."""
        response = self._send(content="", title="Hello", embed=False, mentions=[])
        self.assertEqual(response.status_code, 400)
        self.assertIn("Send as an embed", response.get_json()["error"])
        self.assertEqual(self.standard.sent, [], "an empty message must not reach Discord")

        # …and with the embed ticked, a title on its own is a real message.
        ok = self._send(content="", title="Hello", embed=True, mentions=[])
        self.assertTrue(ok.get_json()["success"], ok.get_json())
        self.assertEqual(self.standard.sent[-1]["embed"].title, "Hello")

    def test_an_embed_keeps_the_tags_out_of_the_description(self):
        """Mentions inside an embed do not notify, so they stay in the content."""
        resp = self._send(
            content="Patch notes are live", title="Update", embed=True, mentions=[str(MEMBER_ID)]
        )
        self.assertTrue(resp.get_json()["success"])
        posted = self.standard.sent[0]
        self.assertEqual(posted["embed"].title, "Update")
        self.assertEqual(posted["embed"].description, "Patch notes are live")
        self.assertNotIn("<@", posted["embed"].description)
        self.assertIn(f"<@{MEMBER_ID}>", posted["content"])

    def test_tags_that_are_not_in_this_guild_are_reported_never_pinged(self):
        resp = self._send(content="hi", mentions=["999999999", "not-a-snowflake"])
        data = resp.get_json()
        self.assertTrue(data["success"])
        posted = self.standard.sent[0]
        self.assertEqual(posted["allowed_mentions"].users, [], "an unknown id is not a ping")
        self.assertEqual(sorted(data["ignored_mentions"]), ["999999999", "not-a-snowflake"])
        self.assertNotIn("<@999999999>", posted["content"])

    def test_a_member_the_cache_has_never_seen_can_still_be_tagged(self):
        """A tag is an id, so an uncached member is a lookup, not a refusal.

        Without the Members intent the cache holds only the people the bot has
        happened to run into; answering "not in this server" for somebody who is
        in the server is what that used to produce.
        """
        unseen = _Member(MEMBER_ID + 99, "newcomer", "Newcomer")
        self.guild.fetchable[unseen.id] = unseen

        response = self._send(content="welcome aboard", mentions=[str(unseen.id)])
        data = response.get_json()

        self.assertTrue(data["success"], data)
        self.assertEqual(data["ignored_mentions"], [])
        posted = self.standard.sent[-1]
        self.assertIn(f"<@{unseen.id}>", posted["content"])
        self.assertEqual([u.id for u in posted["allowed_mentions"].users], [unseen.id])

    def test_one_send_fans_out_to_several_channels(self):
        response = self._send(
            channel_ids=[CHANNEL_ID, self.news.id], content="everyone look", mentions=[]
        ).get_json()
        self.assertTrue(response["success"])
        self.assertEqual(len(response["sent"]), 2)
        self.assertEqual(len(self.standard.sent), 1)
        self.assertEqual(len(self.news.sent), 1)

    def test_a_channel_without_permission_fails_alone(self):
        response = self._send(channel_ids=[CHANNEL_ID, QUIET_CHANNEL_ID], content="hi").get_json()
        self.assertTrue(response["success"], "the channel that worked is still a success")
        self.assertEqual(len(response["sent"]), 1)
        self.assertEqual(len(response["failed"]), 1)
        self.assertIn("cannot send", response["failed"][0]["error"])
        self.assertEqual(self.quiet.sent, [])

    def test_a_channel_that_does_not_exist_is_not_sent_to(self):
        response = self._send(channel_ids=[999999999], content="hi").get_json()
        self.assertFalse(response["success"])
        self.assertEqual(self.standard.sent, [])
        self.assertIn("no longer exists", response["failed"][0]["error"])

    def test_a_refused_send_is_reported(self):
        self.standard.send_error = discord.Forbidden(
            SimpleNamespace(status=403, reason="Missing Permissions"), "nope"
        )
        response = self._send(content="hi").get_json()
        self.assertFalse(response["success"])
        self.assertIn("refused", response["failed"][0]["error"])

    def test_the_result_names_the_message_that_was_sent(self):
        data = self._send(content="hi").get_json()
        self.assertIn("discord.com", data["sent"][0]["jump_url"])
        self.assertEqual(data["sent"][0]["name"], "announcements")

    # ── validation ───────────────────────────────────────────────────────
    def test_an_empty_message_is_refused(self):
        response = self._send(content="   ")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Write something", response.get_json()["error"])
        self.assertEqual(self.standard.sent, [])

    def test_at_least_one_channel_is_required(self):
        self.assertEqual(self._send(channel_ids=[]).status_code, 400)

    def test_too_many_channels_is_refused(self):
        response = self._send(channel_ids=[CHANNEL_ID] * 1 + list(range(1, 10)))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.standard.sent, [])

    def test_a_message_that_fits_but_does_not_fit_with_the_tags_is_explained(self):
        limit = self.keep_alive.MESSAGE_MAX_CHARS
        ping = f"<@{MEMBER_ID}>"
        # The body alone is legal; the tag's newline pushes the total past 2000.
        response = self._send(content="x" * (limit - len(ping)), mentions=[str(MEMBER_ID)])
        self.assertEqual(response.status_code, 400)
        self.assertIn("once the tags are added", response.get_json()["error"])
        self.assertEqual(self.standard.sent, [], "nothing is posted, and nothing is silently cut")

        # Exactly the limit is fine — the boundary is off-by-one-proof both ways.
        fits = self._send(content="x" * (limit - len(ping) - 1), mentions=[str(MEMBER_ID)])
        self.assertTrue(fits.get_json()["success"])
        self.assertEqual(len(self.standard.sent[-1]["content"]), limit)

    def test_an_over_long_message_is_refused_before_anything_is_built(self):
        response = self._send(content="x" * 2001)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.standard.sent, [])

    def test_too_many_tags_is_refused(self):
        response = self._send(mentions=[str(i) for i in range(40)])
        self.assertEqual(response.status_code, 400)

    def test_the_same_client_cannot_flood_the_channels(self):
        limit = self.keep_alive.SEND_MAX_PER_WINDOW
        for _ in range(limit):
            self.assertTrue(self._send(content="spam").get_json()["success"])
        response = self._send(content="spam")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(self.standard.sent), limit, "the refused one is not delivered")

    def test_the_flood_limit_is_a_window_not_a_lifetime_ban(self):
        import time

        limit = self.keep_alive.SEND_MAX_PER_WINDOW
        for _ in range(limit):
            self._send(content="spam")
        self.assertEqual(self._send(content="spam").status_code, 429)
        time.sleep(0.02)
        with mock.patch.object(self.keep_alive, "SEND_WINDOW_SECONDS", 0):
            self.assertTrue(self._send(content="spam").get_json()["success"])

    def test_an_offline_bot_is_not_a_silent_no_op(self):
        self.keep_alive.BOT_INSTANCE._has_started = False
        response = self._send(content="hi")
        self.assertEqual(response.status_code, 400)
        self.assertIn("No server available", response.get_json()["error"])


def render_dashboard(**overrides) -> str:
    """The shipped dashboard template, rendered with a plausible context.

    A module-level function rather than a method so another module can render the
    same page without importing a ``TestCase`` — importing one makes pytest collect
    its tests again in the importing file.
    """
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    import keep_alive

    env = Environment(
        loader=FileSystemLoader("templates"),
        autoescape=select_autoescape(["html"]),
    )
    # The templates ask the *app* for a cache-busting token (`asset_version`),
    # because `/static/*` is served immutable for a year. This environment is
    # not the app, so it has to be handed the same function — otherwise every
    # page that links the stylesheet fails to render here while working
    # perfectly in production, which is the least useful direction for a test
    # to be wrong in.
    env.globals["asset_version"] = keep_alive.asset_version
    context = dict(
        guild_id=GUILD_ID,
        guild_name="Meow Server",
        member_count=3,
        bot_ping=12,
        bot_ready=True,
        modules=[],
        active_count=0,
        total_count=0,
        csrf_token=CSRF,
        guilds=[],
        can_switch_guild=False,
        # The two panels the dashboard adds for one server: the bot's profile
        # there (Discord's per-guild member fields) and the command list that
        # server is registered with. Carried here because the page needs them
        # from the view — the template has no defaults of its own, on purpose,
        # so a route that forgot them would fail loudly rather than render a
        # panel that silently says nothing.
        server_identity={
            "nick": "", "bio": "", "has_avatar": False, "has_banner": False,
            "live_name": "Meow", "live": True, "can_edit": True,
            "max_nick": 32, "max_bio": 190,
        },
        command_scope={
            "total": 90, "available": 90, "hidden": 0,
            "hidden_names": [], "modules": {},
        },
        # The composer's rulebook, which the view hands over from the constants
        # the endpoint validates with. Read from ``keep_alive`` here rather than
        # restated, so this fixture cannot pin numbers the bot does not use.
        composer_limits={
            "chars": keep_alive.MESSAGE_MAX_CHARS,
            "tags": keep_alive.MESSAGE_MAX_MENTIONS,
            "channels": keep_alive.MESSAGE_MAX_CHANNELS,
            "title": keep_alive.MESSAGE_TITLE_MAX,
        },
    )
    context.update(overrides)
    return env.get_template("dashboard.html").render(**context)


class DashboardTemplateCase(unittest.TestCase):
    """The shipped page must actually carry the composer, and carry it safely."""

    render = staticmethod(render_dashboard)

    def test_the_page_hands_the_guild_id_over_as_a_string(self):
        """Same overflow, same fix: this id is posted back with every request."""
        html = self.render()
        self.assertIn(f'const GUILD_ID = "{GUILD_ID}"', html)
        self.assertNotIn(f"const GUILD_ID = {GUILD_ID};", html)

    def test_a_special_tag_is_not_labelled_with_two_at_signs(self):
        """@everyone already carries its @; adding another renders @@everyone."""
        html = self.render()
        self.assertIn('item.label.startsWith("@")', html)
        self.assertNotIn('kind === "Members" ? item.label :', html, "the doubled @ is back")

    def test_the_composer_controls_are_on_the_page(self):
        html = self.render()
        for element_id in (
            "composer-channels",
            "composer-body",
            "composer-title",
            "composer-embed",
            "composer-mention-search",
            "composer-mention-results",
            "composer-chips",
            "composer-counter",
            "composer-preview",
            "composer-send",
            "composer-clear",
        ):
            self.assertIn(f'id="{element_id}"', html, element_id)

    def test_the_page_script_parses_in_a_real_js_engine(self):
        """Nothing else notices a syntax error: the page renders, the CSS applies
        and every control on it is dead — which is exactly how a stray quote in
        one string shipped to production."""
        import os
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        scripts = re.findall(r"<script>(.*?)</script>", self.render(), re.S)
        self.assertTrue(scripts, "the dashboard has no script block")
        for script in scripts:
            handle = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
            try:
                handle.write(script)
                handle.close()
                result = subprocess.run(
                    [node, "--check", handle.name], capture_output=True, text=True
                )
            finally:
                os.unlink(handle.name)
            self.assertEqual(result.returncode, 0, result.stderr[:800])

    @staticmethod
    def _script_slice(script: str, start: str, end: str) -> str:
        """The page's own source between two anchors, or a loud failure.

        A silent miss would let the equality test below pass vacuously.
        """
        begin = script.find(start)
        stop = script.find(end, begin + 1) if begin >= 0 else -1
        if begin < 0 or stop < 0:
            raise AssertionError(f"the page no longer carries {start!r} … {end!r}")
        return script[begin:stop]

    def test_the_pages_rulebook_answers_exactly_what_the_bot_answers(self):
        """The composer's own rules, run against the real validator, case by case.

        The page refuses locally so the user is told before committing to a send —
        and that is only an improvement while both sides give the *same* answer. A
        form that refuses what the bot accepts is a dead end; one that accepts what
        the bot refuses teaches the rule by failing. It was the second kind: the
        character limit was a literal the counter coloured red and nothing acted
        on, and the tag cap did not exist on the page at all.

        So the page's own functions are executed in a real JS engine on stubs, and
        every sentence is compared with ``keep_alive._build_message``'s for the
        equivalent payload. A comparison of strings would have missed the drift
        this is here to catch.
        """
        import json
        import os
        import shutil
        import subprocess
        import tempfile

        import keep_alive

        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")

        html = self.render()
        script = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
        rulebook = self._script_slice(script, "function pingLength()", "function renderPreview()")
        generator = self._script_slice(
            script, "function composerUsesGenerator()", "function applyContentMode()"
        )

        limit = keep_alive.MESSAGE_MAX_CHARS
        tags = keep_alive.MESSAGE_MAX_MENTIONS
        member = f"<@1542296506290143349>"
        member_id = "1542296506290143349"
        one_channel = ["1548437081120833657"]
        cases = [
            # name, payload the browser would post, what it looks like to the page
            ("no channel", {"channel_ids": [], "content": "hi"}, []),
            ("nothing written", {"channel_ids": one_channel, "content": "   "}, one_channel),
            (
                "exactly at the limit",
                {"channel_ids": one_channel, "content": "x" * limit},
                one_channel,
            ),
            (
                "one past the limit",
                {"channel_ids": one_channel, "content": "x" * (limit + 1)},
                one_channel,
            ),
            (
                "fits alone, not with the tag",
                {
                    "channel_ids": one_channel,
                    "content": "x" * (limit - len(member)),
                    "mentions": [member_id],
                },
                one_channel,
            ),
            (
                "a title with no embed",
                {"channel_ids": one_channel, "content": "", "title": "Notice"},
                one_channel,
            ),
            (
                "too many tags",
                {
                    "channel_ids": one_channel,
                    "content": "hi",
                    "mentions": [str(1000 + i) for i in range(tags + 1)],
                },
                one_channel,
            ),
            (
                "an over-long body in embed mode",
                {"channel_ids": one_channel, "content": "x" * (limit + 5), "embed": True},
                one_channel,
            ),
            ("a sendable message", {"channel_ids": one_channel, "content": "hello"}, one_channel),
        ]

        program = "\n".join([
            "const COMPOSER_LIMITS = " + json.dumps({
                "chars": limit,
                "tags": tags,
                "channels": keep_alive.MESSAGE_MAX_CHANNELS,
                "title": keep_alive.MESSAGE_TITLE_MAX,
            }) + ";",
            "let scheduleMode = 'now';",
            "let composerBusy = false;",
            "let composerMentions = [];",
            "let composerSelection = [];",
            "const composerEls = {body: {value: ''}, title: {value: ''},",
            "  embed: {checked: false}, content: {value: ''},",
            "  blocked: {textContent: '', hidden: true}, send: {disabled: false}};",
            rulebook,
            generator,
            "const CASES = " + json.dumps([case[1] for case in cases]) + ";",
            "const PAGE = CASES.map(function (c) {",
            "  composerSelection = (c.channel_ids || []).map(String);",
            "  composerMentions = (c.mentions || []).map(function (id) {",
            "    return {id: id, label: id, kind: 'user'};",
            "  });",
            "  composerEls.body.value = c.content || '';",
            "  composerEls.title.value = c.title || '';",
            "  composerEls.embed.checked = !!c.embed;",
            "  scheduleMode = c.generator ? 'later' : 'now';",
            "  composerEls.content.value = c.generator || '';",
            "  return composerRuleBreak();",
            "});",
            "console.log(JSON.stringify(PAGE));",
        ])

        class _Member:
            id = int(member_id)
            mention = member

        class _Guild:
            """Only what ``_build_message`` reads: one member it can resolve."""

            def get_member(self, user_id):
                return _Member() if str(user_id) == member_id else None

            def get_role(self, user_id):
                return None

            async def fetch_member(self, user_id):
                return None

        handle = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
        try:
            handle.write(program)
            handle.close()
            result = subprocess.run(
                [node, handle.name], capture_output=True, text=True, timeout=60
            )
        finally:
            os.unlink(handle.name)
        self.assertEqual(result.returncode, 0, result.stderr[:800])
        page = json.loads(result.stdout)

        self.assertEqual(len(page), len(cases))
        for (name, payload, _), page_answer in zip(cases, page):
            data = dict(payload)
            if data.get("mentions"):
                data["mentions"] = [str(m) for m in data["mentions"]]
            _, _, bot_answer = keep_alive._build_message(_Guild(), data)
            self.assertEqual(
                page_answer, bot_answer,
                f"{name}: the page says {page_answer!r}, the bot says {bot_answer!r}",
            )

    def test_every_element_the_script_reaches_for_exists(self):
        """``getElementById`` on an id the markup does not have is ``null``, and
        the control it belongs to does nothing at all until someone clicks it."""
        html = self.render()
        looked_up = set(re.findall(r'getElementById\("([^"]+)"\)', html))
        self.assertTrue(looked_up, "no element lookups found — has the script moved?")
        missing = sorted(name for name in looked_up if f'id="{name}"' not in html)
        self.assertEqual(missing, [], "the script looks up ids the page never renders")

    def test_the_channel_picker_is_not_a_ctrl_click_select(self):
        """A ``<select multiple>`` needs Ctrl-click, cannot be searched, hides what
        is selected and does nothing on a touch screen."""
        html = self.render()
        self.assertNotIn('<select id="composer-channels"', html)
        self.assertIn('id="composer-channels"', html)
        self.assertIn('aria-multiselectable="true"', html)
        self.assertIn('id="composer-channel-search"', html)
        self.assertIn('id="composer-channel-count"', html)
        self.assertNotIn("Ctrl/Cmd-click", html)

    def _receipt_summary(self, result, carried=None):
        """Run the page's own receipt logic in a real JavaScript engine.

        The summary is deliberately a pure function of the server's reply, so it
        can be executed on its own: what a send *looks like* afterwards is half of
        what this feature is, and reading the JavaScript is not evidence that it
        works.
        """
        import json
        import os
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        match = re.search(
            r"/\* receipt-summary:start \*/(.*?)/\* receipt-summary:end \*/", self.render(), re.S
        )
        self.assertIsNotNone(match, "the receipt summary is no longer marked for extraction")
        script = (
            f"const result = {json.dumps(result)};\n"
            f"const carried = {json.dumps(carried or [])};\n"
            f"{match.group(1)}\n"
            "console.log(JSON.stringify(receiptSummary(result, carried)));"
        )
        handle = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
        try:
            handle.write(script)
            handle.close()
            run = subprocess.run([node, handle.name], capture_output=True, text=True)
        finally:
            os.unlink(handle.name)
        self.assertEqual(run.returncode, 0, run.stderr[:600])
        return json.loads(run.stdout)

    def test_a_clean_send_reads_as_a_receipt(self):
        summary = self._receipt_summary({
            "success": True,
            "sent": [
                {"channel_id": "1", "name": "general", "jump_url": "https://discord.com/channels/1/1/2"},
                {"channel_id": "2", "name": "updates", "jump_url": "https://discord.com/channels/1/2/3"},
            ],
            "failed": [],
            "ignored_mentions": [],
        })
        self.assertEqual(summary["title"], "Sent to all 2 channels")
        self.assertEqual(summary["tone"], "ok")
        self.assertEqual(summary["retry_ids"], [], "nothing to retry")
        self.assertEqual([row["name"] for row in summary["rows"]], ["#general", "#updates"])
        for row in summary["rows"]:
            self.assertTrue(row["jump_url"].startswith("https://discord.com/"), row)

    def test_a_partial_send_names_what_failed_and_what_a_retry_would_reach(self):
        summary = self._receipt_summary({
            "success": True,
            "sent": [{"channel_id": "1", "name": "general", "jump_url": "https://discord.com/x"}],
            "failed": [{
                "channel_id": "555002",
                "name": "staff-only",
                "error": "I cannot send messages in #staff-only",
            }],
            "ignored_mentions": [],
        })
        self.assertEqual(summary["title"], "Sent to 1 of 2 channels")
        self.assertEqual(summary["tone"], "warn", "a half-done send is not painted green")
        self.assertEqual(summary["retry_ids"], ["555002"], "only what failed is retried")
        self.assertIn("I cannot send messages in #staff-only", [row["why"] for row in summary["rows"]])
        self.assertTrue(summary["note"], "a retry has to say that it repeats the message")

    def test_a_send_that_reached_nothing_is_not_called_a_success(self):
        summary = self._receipt_summary({
            "success": False,
            "sent": [],
            "failed": [{"channel_id": "999", "error": "That channel no longer exists"}],
            "ignored_mentions": [],
        })
        self.assertEqual(summary["title"], "Not sent")
        self.assertEqual(summary["tone"], "fail")
        self.assertEqual(summary["rows"][0]["name"], "#999", "an unnamed channel is still named")
        self.assertEqual(summary["retry_ids"], ["999"])

    def test_a_dropped_tag_is_part_of_the_result(self):
        summary = self._receipt_summary({
            "success": True,
            "sent": [{"channel_id": "1", "name": "general", "jump_url": "https://discord.com/x"}],
            "failed": [],
            "ignored_mentions": ["1549177590328205393"],
        })
        self.assertEqual(summary["title"], "Sent", "the message did go out")
        self.assertEqual(summary["rows"][-1]["tone"], "note")
        self.assertIn("not tagged", summary["rows"][-1]["why"])

    def test_a_retry_does_not_erase_what_already_arrived(self):
        """The receipt is the record of the *message*: a retry that hides the
        channels which already took it is the same forgetting this rebuild is for."""
        summary = self._receipt_summary(
            {
                "success": True,
                "sent": [{"channel_id": "2", "name": "updates", "jump_url": "https://discord.com/y"}],
                "failed": [],
                "ignored_mentions": [],
            },
            carried=[{"channel_id": "1", "name": "general", "jump_url": "https://discord.com/x"}],
        )
        self.assertEqual(summary["title"], "Sent to all 2 channels")
        self.assertEqual([row["name"] for row in summary["rows"]], ["#general", "#updates"])
        self.assertEqual(summary["retry_ids"], [])

    def test_a_channel_is_never_counted_twice(self):
        """An id already on the receipt and named again in the reply is one row."""
        same = {"channel_id": "1", "name": "general", "jump_url": "https://discord.com/x"}
        summary = self._receipt_summary(
            {"success": True, "sent": [same], "failed": [], "ignored_mentions": []},
            carried=[same],
        )
        self.assertEqual(len(summary["rows"]), 1)
        self.assertEqual(summary["title"], "Sent")

    def test_the_send_does_not_treat_a_delivered_report_as_an_error(self):
        """``success: false`` from this endpoint is a real answer — a send that
        reached nobody — and the receipt has to be able to show it. ``apiPost``
        throws on it, which reported that case as "Request failed (200)"."""
        html = self.render()
        self.assertIn("apiPostResult(\"/api/send-message\"", html)
        self.assertIn("if (!response.ok) throw new Error", html)
        self.assertNotIn("apiPost(\"/api/send-message\"", html)

    def test_an_empty_reply_is_not_reported_as_success(self):
        summary = self._receipt_summary({"success": False})
        self.assertEqual(summary["title"], "Nothing was sent")
        self.assertEqual(summary["tone"], "fail")
        self.assertEqual(summary["retry_ids"], [])

    def test_the_send_result_is_a_receipt_and_not_a_disappearing_toast(self):
        """Three seconds of toast is not a record of a sent, unrecoverable message."""
        html = self.render()
        for element_id in (
            "composer-receipt",
            "composer-receipt-title",
            "composer-receipt-rows",
            "composer-receipt-foot",
            "composer-receipt-note",
            "composer-retry",
            "composer-receipt-dismiss",
        ):
            self.assertIn(f'id="{element_id}"', html, element_id)
        self.assertNotIn("showToast(`#${item.name", html, "a refusal must not fade after three seconds")
        self.assertNotIn("showToast(`Sent to ", html)

    def test_a_mass_ping_is_warned_about_before_it_is_sent(self):
        html = self.render()
        self.assertIn("notifies the whole server", html)
        self.assertIn("window.confirm", html)
        self.assertIn("massPingKeyword", html)

    def test_the_picker_marks_a_voice_channel_as_one(self):
        html = self.render()
        self.assertIn('channel.kind === "voice"', html, "the row has to look like a voice channel")
        self.assertIn("text chat turned on", html, "and the one way it can refuse is said up front")

    def test_the_page_states_the_servers_own_limits_and_no_others(self):
        """Two copies of a limit drift; the page must not offer a sixth channel
        the endpoint would then refuse — nor count to a 2,000 the bot no longer
        has. Every number the composer states comes out of the rulebook the view
        builds from the same constants the endpoint validates with.
        """
        import keep_alive

        html = self.render()
        for name, value in (
            ("chars", keep_alive.MESSAGE_MAX_CHARS),
            ("tags", keep_alive.MESSAGE_MAX_MENTIONS),
            ("channels", keep_alive.MESSAGE_MAX_CHANNELS),
            ("title", keep_alive.MESSAGE_TITLE_MAX),
        ):
            self.assertIn(f"{name}: {value},", html, f"the page does not carry the bot's {name}")
        self.assertIn(f"up to {keep_alive.MESSAGE_MAX_CHANNELS}", html)

    def test_a_different_rulebook_reaches_the_page(self):
        """The page has no limits of its own: change them and the page changes.

        Written because they used to be literals in the template — the counter
        said 2000 because the template said 2000 — so a page that ignores what
        the view hands it looks exactly like a correct one, right up until the
        constant moves and the page goes on stating the old number.
        """
        html = self.render(
            composer_limits={"chars": 1234, "tags": 7, "channels": 3, "title": 99}
        )
        for fragment in (
            "chars: 1234,", "tags: 7,", "channels: 3,", "title: 99,",
            'maxlength="1234"', 'maxlength="99"', ">0 / 1234<",
            "up to 3",
            # The counter's threshold is that number, not a second copy of it.
            "total > COMPOSER_LIMITS.chars",
        ):
            self.assertIn(fragment, html)
        # A number the template still states for itself would survive this render.
        # Everything after the stylesheet is the page: the CSS ``z-index: 2000``
        # is not a message limit, so it is excluded by looking past ``</style>``.
        self.assertNotIn(
            "2000", html.split("</style>", 1)[1],
            "a limit the template states for itself is back",
        )

    #: The dashboard's own rules, which used to be a `<style>` block in the
    #: template and are now a linked sheet. The hidden-element guards below read
    #: it *as well as* the template, because a rule that sets `display` is
    #: exactly as authoritative from a linked sheet as from an inline one — and
    #: reading only the template would have left this check passing while it
    #: checked almost nothing, which is the failure it was written to catch.
    STYLESHEET = os.path.join("static", "dashboard.css")

    @classmethod
    def _with_stylesheet(cls, markup: str) -> str:
        try:
            with open(cls.STYLESHEET, encoding="utf-8") as handle:
                return markup + "\n" + handle.read()
        except OSError:
            return markup

    @staticmethod
    def _unguarded_hidden(html: str) -> list[str]:
        """Classes that carry ``hidden`` and their own ``display``, unguarded.

        A rule is only needed for a class that declares its own ``display``,
        because the user-agent's ``[hidden]`` rule loses to any author one.
        """
        missing = []
        for element in re.finditer(r"<[^>]*\shidden[^>]*>", html):
            classes = re.search(r'class="([^"]*)"', element.group(0))
            for name in (classes.group(1).split() if classes else []):
                block = re.search(
                    r"^\s*\.%s\s*\{(.*?)\}" % re.escape(name), html, re.S | re.M
                )
                if not block or "display:" not in block.group(1):
                    continue
                if not re.search(r"\.%s\[hidden\]" % re.escape(name), html):
                    missing.append(f".{name}")
        return missing

    def test_anything_the_script_hides_is_actually_hidden(self):
        """``display: flex`` outranks the browser's own ``[hidden]`` rule.

        The page already carried three ``[hidden]`` guards for exactly this reason,
        and the fourth was missing: the receipt's footer is set ``hidden`` when
        there is nothing to retry, and stayed on screen offering to "Retry the 0
        failed channels". A rule is only needed for a class that declares its own
        ``display``, which is what this checks — the whole family, not the one case.
        """
        missing = self._unguarded_hidden(self._with_stylesheet(self.render()))
        self.assertEqual(
            missing, [], f"hidden elements whose own rule sets display, with no guard: {missing}"
        )

    def test_anything_the_server_can_hide_is_actually_hidden(self):
        """The same trap through the *other* door: the server's own ``hidden``.

        An element with ``{% if ... %} hidden{% endif %}`` only carries the
        attribute in the renders where its condition is true, so a fixture whose
        state has nothing hidden cannot see it at all. That is how the command
        list's "every command the bot has is offered on this server" note — a
        ``.field-head``, so ``display: flex`` — shipped saying exactly that
        directly above the thirty-one names that were not offered, in the page's
        own render and in the live refresh that sets ``hidden`` afterwards.

        Read from the template source, therefore, not from one render.
        """
        source = open("templates/dashboard.html", encoding="utf-8").read()
        missing = self._unguarded_hidden(self._with_stylesheet(source))
        self.assertEqual(
            missing, [],
            f"elements the server can hide, whose own rule sets display, with no "
            f"guard: {missing}",
        )

    def test_the_page_calls_the_composer_endpoints(self):
        html = self.render()
        for endpoint in ("/api/channels", "/api/mentionables", "/api/send-message"):
            self.assertIn(endpoint, html, endpoint)

    def test_the_embed_title_is_hidden_until_embed_mode_is_chosen(self):
        html = self.render()
        self.assertIn('id="composer-title-field" hidden', html)

    def test_names_are_rendered_as_text_never_as_markup(self):
        """A member called ``<b>`` must stay a name — the page builds no HTML
        from anything the server or Discord returns."""
        html = self.render()
        self.assertNotIn("innerHTML +=", html)
        self.assertNotIn("innerHTML = `", html)
        self.assertNotIn("insertAdjacentHTML", html)
        # Filled through createElement/textContent instead.
        self.assertIn('preview.innerHTML = ""', html)
        self.assertIn("name.textContent =", html)

    def test_the_offline_bot_is_advertised_before_anything_is_typed(self):
        html = self.render()
        self.assertIn("const BOT_READY = true", html)
        self.assertIn("The bot is offline", html)


if __name__ == "__main__":
    unittest.main()
