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
        # Real roles carry both; a stand-in that invented a *different* answer
        # would let a law pass here and fail in Discord. `managed` is what
        # `permissions.role_grant_problem` asks before it lets anybody wire a
        # reaction-role mapping to a role Discord will not hand out.
        self.managed = False
        self.position = 0

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
        # The window belongs to the limiter object rather than to the module, so
        # it is the thing patched — the claim being tested is that the *same* key
        # is admitted again once its window has passed, and that is a property of
        # the window, wherever it is stored. (``keep_alive.SEND_WINDOW_SECONDS``
        # is the configured value the limiter was built from; patching it would
        # change the module and nothing else.)
        with mock.patch.object(self.keep_alive._SEND, "window_seconds", 0):
            self.assertTrue(self._send(content="spam").get_json()["success"])

    def test_an_offline_bot_is_not_a_silent_no_op(self):
        self.keep_alive.BOT_INSTANCE._has_started = False
        response = self._send(content="hi")
        self.assertEqual(response.status_code, 400)
        self.assertIn("No server available", response.get_json()["error"])


def support_card(*, on=True, postable=True, readable=True, rows=None, read=True) -> dict:
    """One support-bridge card, in the shape ``utils.support`` answers with.

    Built by the module's own readers over a stand-in channel rather than read
    from a database, because this fixture renders a template and nothing else —
    the *decisions* (is it on, may the bot post, is the transcript readable) are
    still the module's, so a page test drives the branches by parametrising this
    rather than by inventing a shape production does not answer with.
    """
    from utils import support

    channel = SimpleNamespace(
        id=support.channel_id(), name="support", mention="#support"
    )
    channel.permissions_for = lambda _member: SimpleNamespace(
        send_messages=postable, read_message_history=readable
    )
    channel.guild = SimpleNamespace(me=SimpleNamespace(id=999))
    # The module's own readers either way: "off" here is the module's off switch
    # (a channel id of 0) rather than a boolean this fixture made up.
    settings = {"SUPPORT_CHANNEL_ID": "0"} if not on else {}
    with mock.patch.dict(os.environ, settings):
        status = support.status(find=lambda _cid: channel)
    if rows is None:
        rows = [
            support.reading({
                "id": 1, "message_id": 100, "side": support.DISCORD,
                "author_id": 5, "author_name": "Ash",
                "body": "the export fails on a big server",
                "attachments": 0, "jump": "https://discord.com/channels/1/2/3",
                "where_name": "", "ts": 1_700_000_000,
            }),
            support.reading({
                "id": 2, "message_id": 101, "side": support.WEB,
                "author_id": 999, "author_name": "Raj",
                "body": "looking at it now", "attachments": 0, "jump": "",
                "where_name": "", "ts": 1_700_000_100,
            }),
        ]
    transcript = {
        "read": read,
        "why": "" if read else "The database could not be opened.",
        "rows": rows if read else [],
        "total": len(rows),
        "shown": len(rows) if read else 0,
    }
    return {"status": status, "transcript": transcript, "url": "/api/support"}


def render_dashboard(**overrides) -> str:
    """The shipped dashboard template, rendered with a plausible context.

    A module-level function rather than a method so another module can render the
    same page without importing a ``TestCase`` — importing one makes pytest collect
    its tests again in the importing file.
    """
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    import keep_alive
    from utils import brand, command_publish

    env = Environment(
        loader=FileSystemLoader("templates"),
        autoescape=select_autoescape(["html"]),
    )
    # …and the brand, for the same reason and with a sharper failure: the frame's
    # `<meta name="theme-color">` reads `brand.BG_HEX`, and a bare environment
    # without the global raises `'brand' is undefined` rather than rendering an
    # empty colour — which is the loud direction, and the one to keep.
    env.globals["brand"] = brand
    # The templates ask the *app* for a cache-busting token (`asset_version`),
    # because `/static/*` is served immutable for a year. This environment is
    # not the app, so it has to be handed the same function — otherwise every
    # page that links the stylesheet fails to render here while working
    # perfectly in production, which is the least useful direction for a test
    # to be wrong in.
    env.globals["asset_version"] = keep_alive.asset_version
    # Which page to draw. The dashboard is eleven addresses now, and one render
    # draws one of them — so the fixture is told which, and the page's own copy,
    # its anchor and the rail all follow from the same table the routes use.
    page = overrides.pop("page", overrides.get("dash_page", "overview"))
    nav = keep_alive._page_nav(
        keep_alive.DASHBOARD_PAGES,
        page,
        f"?guild_id={GUILD_ID}",
        groups=keep_alive.DASHBOARD_GROUPS,
    )
    title, lede = keep_alive.DASHBOARD_PAGE_COPY.get(
        page, keep_alive.DASHBOARD_PAGE_COPY["overview"]
    )
    context = dict(
        dash_page=page,
        page_title=title,
        page_lede=lede,
        page_anchor=keep_alive._page_anchor(keep_alive.DASHBOARD_PAGES, page),
        page_url=f"/dashboard?guild_id={GUILD_ID}",
        rail=keep_alive._dashboard_rail(nav),
        # A write's sentence, read once by the page that drew it. Empty here: an
        # announcement is what a form's own redirect carries, not what a render
        # with no session has.
        dash_notice="",
        dash_error="",
        guild_id=GUILD_ID,
        guild_name="Meow Server",
        member_count=3,
        bot_ping=12,
        bot_ready=True,
        # Read from the view rather than written out, like the limits above: the
        # badge is a partial both consoles include, so a fixture with its own copy
        # of the three states would pin a shape production does not use.
        gateway=keep_alive._badge_state(),
        modules=[],
        active_count=0,
        total_count=0,
        csrf_token=CSRF,
        guilds=[],
        can_switch_guild=False,
        # No `page`: the dashboard is seven pages in production and one document
        # in this environment, which is what `page is not defined` means in the
        # template — everything renders, and every test written against this
        # fixture goes on reading the whole thing. The rail is still carried,
        # from the same list the routes are registered from, so the page's
        # navigation is not the one thing a renderer has to guess.
        dashboard_pages=keep_alive._page_nav(keep_alive.DASHBOARD_PAGES, ""),
        pages_json=keep_alive._inline_json({
            page_id: {"url": path, "label": label}
            for page_id, label, path, _anchor in keep_alive.DASHBOARD_PAGES
        }),
        guild_query="",
        page_href="/dashboard?guild_id=",
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
        # The other half of that panel: what Discord holds, which the *view*
        # supplies and the page only prints. Built by the module that owns the
        # wording rather than written out here, for the same reason the limits
        # above are read from ``keep_alive`` — a fixture with its own copy pins
        # a sentence production does not use.
        command_scope_publish=command_publish.describe(GUILD_ID),
        # The composer's rulebook, which the view hands over from the constants
        # the endpoint validates with. Read from ``keep_alive`` here rather than
        # restated, so this fixture cannot pin numbers the bot does not use.
        composer_limits={
            "chars": keep_alive.MESSAGE_MAX_CHARS,
            "tags": keep_alive.MESSAGE_MAX_MENTIONS,
            "channels": keep_alive.MESSAGE_MAX_CHANNELS,
            "title": keep_alive.MESSAGE_TITLE_MAX,
        },
        # Everything the eleven sections read, in the shapes the view hands over.
        # The ledgers and the queue are *readings* rather than booleans — a page
        # test has to be able to drive the empty branch, the unread branch and a
        # populated one, and each of those is a different sentence.
        economy_view=None,
        level_view=None,
        economy_display=None,
        level_display=None,
        economy_fee={"label": "", "enabled": True, "cap": 0, "stored": False, "describe": ""},
        schedule_queue={
            "read": True, "upcoming": [], "recent": [], "pending": 0,
            "max_pending": keep_alive.scheduled.MAX_PENDING_PER_GUILD,
            "catchup_hours": keep_alive.scheduled.MISS_CATCHUP_SECONDS // 3600,
        },
        channel_choices=[],
        # No log channel set, and read: the Server page's picker then draws its
        # own "off" option, which is the state a server starts in.
        log_picker={"read": True, "channel_id": "", "current": ""},
        # The other two settings the Server page draws — the role new members
        # get, and the reaction-role mappings — in the states a server that has
        # configured neither is in. Read, empty, so the page takes its honest
        # branch rather than the one for "Discord could not be asked".
        roles=[],
        default_role={"read": True, "role_id": "", "current": "", "above": False, "offered": True},
        reaction_roles={"read": True, "rows": []},
        # The two action lists, from the tuples the endpoints validate against, so
        # the page cannot offer an action that would be refused.
        member_actions=keep_alive.PANEL_MEMBER_ACTIONS,
        server_actions=keep_alive.PANEL_SERVER_ACTIONS,
        # The two write doors every form posts to, named once by the view rather
        # than typed into eleven forms.
        toggle_url="/api/toggle",
        toggle_all_url="/api/toggle-all",
        # An unread roster: the members and server pages then draw the branch a
        # page takes when Discord could not be asked, which is a sentence.
        management={
            "page": "", "read": False, "why": "", "roster": None, "bans": [],
            "control": None, "query": "", "recent": [], "abilities": None,
        },
        overview_live={
            "read": True, "why": "", "rooms": [], "people": 0, "alone": 0, "radio": None,
        },
        # The checklist the front page draws, in the shape `_server_health`
        # answers with — and with the readings named by the module that decides
        # them rather than typed here, for the reason the limits above are read
        # from ``keep_alive``. A server with nothing to complain about is the case
        # that has to render as clearly as a broken one.
        server_health={
            "read": True,
            "why": "",
            "findings": [],
            "checked": [label for _key, label in keep_alive.health_checks.READINGS],
            "unread": [],
            "counts": {"broken": 0, "warning": 0},
        },
        # The support bridge's card, drawn for the operator this fixture signs in
        # as. `support_card()` is the module's own readers over a stand-in
        # channel; a page test that wants another branch passes its own.
        support=support_card(),
        # The deployment panels, empty: the shapes are the readers' own and a
        # page test about them passes its own fixture.
        system_status={},
        site_guard_status={},
        config_status={},
        alert_state={},
        server_now=1_700_000_000,
        schedule_repeat_choices=[
            {"minutes": minutes, "label": label}
            for minutes, label in keep_alive.scheduled.REPEAT_CHOICES
        ],
        schedule_max_pending=keep_alive.scheduled.MAX_PENDING_PER_GUILD,
        schedule_catchup_hours=keep_alive.scheduled.MISS_CATCHUP_SECONDS // 3600,
        schedule_content_choices=[
            {"value": "", "label": "Message I write", "blurb": ""},
            *[
                {"value": name, "label": entry["label"], "blurb": entry["blurb"]}
                for name, entry in keep_alive.digest.GENERATORS.items()
            ],
        ],
        selling=False,
        fleet_url="",
        pick_server_url="",
        signed_in_as={},
    )
    context.update(overrides)
    return env.get_template("dashboard.html").render(**context)


if __name__ == "__main__":
    unittest.main()
