"""The dashboard composer: the bot posts what an administrator typed.

This is the most powerful surface in the project — it speaks as the bot, in any
channel, tagging anyone — so the tests are about what it *refuses*: a request
with no session, no CSRF token, a tag that is not a member or role of the guild
being written to, a channel the bot cannot send in, and a guild the signed-in
session was not pinned to. Everything the browser sends is re-resolved against
the live guild, because a browser can send anything.
"""
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


class _Channel:
    def __init__(self, cid, name, *, can_send=True, viewable=True, category=None, news=False):
        self.id = cid
        self.name = name
        self.category = SimpleNamespace(name=category) if category else None
        self.slowmode_delay = 0
        self.sent = []
        self.send_error = None
        self._can_send = can_send
        self._viewable = viewable
        self._news = news

    def is_news(self):
        return self._news

    def permissions_for(self, member):
        return SimpleNamespace(view_channel=self._viewable, send_messages=self._can_send)

    async def send(self, content=None, embed=None, allowed_mentions=None, **kwargs):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append({"content": content, "embed": embed, "allowed_mentions": allowed_mentions})
        return SimpleNamespace(
            id=len(self.sent), jump_url=f"https://discord.com/channels/{GUILD_ID}/{self.id}/{len(self.sent)}"
        )


class _Guild:
    def __init__(self, gid, name, channels, *, roles=(), members=()):
        self.id = gid
        self.name = name
        # discord.py has no ``Guild.news_channels``: an announcement channel is a
        # TextChannel whose type is news, so it comes back from ``text_channels``
        # with every other one. Splitting the list in two here is what let a live
        # AttributeError sit behind a green test suite.
        self.text_channels = list(channels)
        self.roles = [_Role(gid, "@everyone", default=True)] + list(roles)
        self.members = list(members)
        self.me = SimpleNamespace(id=999)
        self._channels = {c.id: c for c in channels}

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


class ComposerCase(unittest.TestCase):
    """Driven through Flask's test client, with a stand-in gateway behind it."""

    def setUp(self):
        self.standard = _Channel(CHANNEL_ID, "announcements", category="News")
        self.quiet = _Channel(QUIET_CHANNEL_ID, "staff-only", can_send=False)
        self.hidden = _Channel(OTHER_CHANNEL_ID, "secret", viewable=False)
        self.news = _Channel(555004, "updates", news=True)
        self.role = _Role(ROLE_ID, "Members")
        self.member = _Member(MEMBER_ID, "raj", "RAJ")
        self.guild = _Guild(
            GUILD_ID,
            "Meow Server",
            [self.standard, self.quiet, self.hidden, self.news],
            roles=[self.role],
            members=[self.member],
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
        bookkeeping = {"sent", "send_error"}  # where a faked channel records
        cases = [
            (self.guild, discord.Guild),
            (self.standard, discord.TextChannel),
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

    def test_mentionables_ships_no_member_list_without_a_query(self):
        data = self._get(f"/api/mentionables?guild_id={GUILD_ID}").get_json()
        self.assertEqual(data["users"], [], "a whole member list is never sent to the browser")

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


class DashboardTemplateCase(unittest.TestCase):
    """The shipped page must actually carry the composer, and carry it safely."""

    @staticmethod
    def render() -> str:
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        env = Environment(
            loader=FileSystemLoader("templates"),
            autoescape=select_autoescape(["html"]),
        )
        return env.get_template("dashboard.html").render(
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
        )

    def test_the_page_hands_the_guild_id_over_as_a_string(self):
        """Same overflow, same fix: this id is posted back with every request."""
        html = self.render()
        self.assertIn(f'const GUILD_ID = "{GUILD_ID}"', html)
        self.assertNotIn(f"const GUILD_ID = {GUILD_ID};", html)

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
