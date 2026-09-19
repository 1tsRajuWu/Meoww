"""Signing in as yourself, and reaching only the servers you run.

Both older ways in belong to the operator: a one-time link is scoped to the one
server it was minted in, and the passkey is the whole bot. Neither can answer
"which of my servers is this bot in, and which may I manage", because that answer
depends on the person — so the dashboard grew a third way in, and this file pins
what it may and may not do:

1. **The admin rule.** Discord sends ``permissions`` as a *string*, so a filter
   that compared it to an int would keep every owner and drop every
   administrator — the shape of bug that looks like "the list is just short".
2. **The intersection.** Only servers the person administers *and* the bot is in:
   a server the bot has left has nothing to configure, and offering it makes the
   page look broken rather than empty.
3. **The scope.** A Discord session may reach its own servers and no others, and
   a form naming somebody else's server is refused by name — the same rule the
   hand-edited-cookie case needs.
4. **The setup path.** With no client id and secret, sign-in says exactly that
   and prints the callback URL to register, instead of failing at Discord with a
   redirect-URI mismatch that reads like a bug in the bot.
"""
import os
import unittest
import urllib.parse
from unittest import mock

import keep_alive
from tests.test_dashboard import CSRF, GUILD_ID, _DashboardFixture, make_bot
from utils import discord_auth

OTHER_GUILD_ID = 999002  # the fixture bot is in this one and names it "Other Server"


def _guild(gid, name, *, owner=False, permissions="0", icon=None):
    return {
        "id": str(gid), "name": name, "owner": owner,
        "permissions": permissions, "icon": icon,
    }


class TheAdminRule(unittest.TestCase):
    """Who counts as running a server."""

    def test_an_owner_is_an_administrator(self):
        self.assertTrue(discord_auth.is_admin_of(_guild(1, "a", owner=True), 42))

    def test_manage_server_counts(self):
        bit = str(discord_auth.MANAGE_GUILD)
        self.assertTrue(discord_auth.is_admin_of(_guild(1, "a", permissions=bit), 42))

    def test_administrator_counts(self):
        bit = str(discord_auth.ADMINISTRATOR)
        self.assertTrue(discord_auth.is_admin_of(_guild(1, "a", permissions=bit), 42))

    def test_a_member_with_no_bits_does_not(self):
        self.assertFalse(discord_auth.is_admin_of(_guild(1, "a", permissions="0"), 42))

    def test_a_random_permission_does_not(self):
        # SEND_MESSAGES | VIEW_CHANNEL — a normal member of a normal server.
        self.assertFalse(discord_auth.is_admin_of(_guild(1, "a", permissions="3072"), 42))

    def test_the_combined_bits_are_read_as_numbers_not_strings(self):
        """Discord sends ``permissions`` as a string; the filter must parse it."""
        both = str(discord_auth.MANAGE_GUILD | discord_auth.ADMINISTRATOR | 3072)
        self.assertTrue(discord_auth.is_admin_of(_guild(1, "a", permissions=both), 42))

    def test_rubbish_is_not_a_permission(self):
        for value in ("", None, "not a number", {}, []):
            self.assertFalse(
                discord_auth.is_admin_of(_guild(1, "a", permissions=value), 42),
                f"{value!r} was read as an administrator",
            )

    def test_a_non_guild_is_not_a_guild(self):
        self.assertFalse(discord_auth.is_admin_of(None, 42))
        self.assertFalse(discord_auth.is_admin_of("a server", 42))


class TheServerList(unittest.TestCase):
    """What the picker is allowed to offer."""

    def test_only_servers_the_person_runs(self):
        admin_bit = str(discord_auth.MANAGE_GUILD)
        found = discord_auth.servers_for(
            [
                _guild(1, "Mine", permissions=admin_bit),
                _guild(2, "Not mine", permissions="0"),
            ],
            user_id=7,
            bot_guild_ids=[1, 2],
        )
        self.assertEqual([entry["name"] for entry in found], ["Mine"])

    def test_only_servers_the_bot_is_in(self):
        admin_bit = str(discord_auth.MANAGE_GUILD)
        found = discord_auth.servers_for(
            [
                _guild(1, "Bot is here", permissions=admin_bit),
                _guild(2, "Bot is not", permissions=admin_bit),
            ],
            user_id=7,
            bot_guild_ids=[1],
        )
        self.assertEqual([entry["name"] for entry in found], ["Bot is here"])

    def test_the_order_is_stable_and_readable(self):
        admin_bit = str(discord_auth.MANAGE_GUILD)
        found = discord_auth.servers_for(
            [
                _guild(3, "zebra", permissions=admin_bit),
                _guild(1, "Apple", permissions=admin_bit),
                _guild(2, "banana", permissions=admin_bit),
            ],
            user_id=7,
            bot_guild_ids=[1, 2, 3],
        )
        self.assertEqual([entry["name"] for entry in found], ["Apple", "banana", "zebra"])

    def test_ids_are_strings_so_a_comparison_cannot_miss(self):
        admin_bit = str(discord_auth.MANAGE_GUILD)
        found = discord_auth.servers_for(
            [_guild(5, "Five", permissions=admin_bit)], user_id=7, bot_guild_ids=[5]
        )
        self.assertEqual(found[0]["id"], "5")

    def test_a_namesless_server_still_appears(self):
        admin_bit = str(discord_auth.MANAGE_GUILD)
        entry = _guild(5, "", permissions=admin_bit)
        entry["name"] = None
        found = discord_auth.servers_for([entry], user_id=7, bot_guild_ids=[5])
        self.assertEqual(found[0]["name"], "Unnamed server")

    def test_junk_input_does_not_raise(self):
        self.assertEqual(discord_auth.servers_for(None, 7, [1]), [])
        self.assertEqual(discord_auth.servers_for(["nonsense", None], 7, [1]), [])
        self.assertEqual(discord_auth.servers_for([_guild(1, "a")], 7, None), [])


class TheUrls(unittest.TestCase):
    """The two links, which are the whole external surface of this module."""

    def test_the_sign_in_url_asks_for_identify_and_guilds(self):
        with mock.patch.dict(os.environ, {"DISCORD_CLIENT_ID": "123"}):
            url = discord_auth.authorize_url("https://example.test/cb", "STATE")
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "discord.com")
        self.assertEqual(query["client_id"], ["123"])
        self.assertEqual(query["redirect_uri"], ["https://example.test/cb"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["state"], ["STATE"])
        self.assertEqual(query["scope"], ["identify guilds"])

    def test_the_sign_in_url_asks_for_nothing_else(self):
        with mock.patch.dict(os.environ, {"DISCORD_CLIENT_ID": "123"}):
            url = discord_auth.authorize_url("https://example.test/cb", "STATE")
        self.assertNotIn("email", url)
        self.assertNotIn("messages", url)
        self.assertNotIn("connections", url)

    def test_the_invite_asks_for_commands_as_well_as_the_bot(self):
        with mock.patch.dict(os.environ, {"DISCORD_CLIENT_ID": "123"}):
            url = discord_auth.invite_url()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["scope"], ["bot applications.commands"])
        # This asserted `str(discord_auth.ADMINISTRATOR)` — and that assertion is
        # why the blanket grant survived: the grant was pinned, but only to
        # itself, so nothing ever asked whether it was the right one. What
        # matters is that the link carries the named list, which
        # `InvitePermissionsCase` decodes and checks entry by entry.
        self.assertEqual(query["permissions"], [str(discord_auth.INVITE_PERMISSIONS)])
        self.assertNotIn("guild_id", query)

    def test_the_invite_can_name_its_server(self):
        with mock.patch.dict(os.environ, {"DISCORD_CLIENT_ID": "123"}):
            url = discord_auth.invite_url(guild_id=4242)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["guild_id"], ["4242"])
        self.assertEqual(query["disable_guild_select"], ["true"])

    def test_a_server_named_zero_is_not_a_server(self):
        with mock.patch.dict(os.environ, {"DISCORD_CLIENT_ID": "123"}):
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(discord_auth.invite_url(guild_id=0)).query
            )
        self.assertNotIn("guild_id", query)

    def test_discord_sign_in_needs_both_halves(self):
        cases = [
            ({}, False),
            ({"DISCORD_CLIENT_ID": "123"}, False),
            ({"DISCORD_CLIENT_SECRET": "shh"}, False),
            ({"DISCORD_CLIENT_ID": "123", "DISCORD_CLIENT_SECRET": "shh"}, True),
            ({"DISCORD_CLIENT_ID": "  ", "DISCORD_CLIENT_SECRET": "shh"}, False),
        ]
        for env, expected in cases:
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(discord_auth.enabled(), expected, env)

    def test_a_picture_is_a_picture_or_nothing(self):
        self.assertEqual(discord_auth.icon_url({"id": "1", "icon": None}), "")
        self.assertIn(".png", discord_auth.icon_url({"id": "1", "icon": "abc"}))
        self.assertIn(".gif", discord_auth.icon_url({"id": "1", "icon": "a_abc"}))
        self.assertEqual(discord_auth.avatar_url({"id": "1", "avatar": None}), "")

    def test_the_display_name_prefers_what_people_are_called_now(self):
        self.assertEqual(
            discord_auth.display_name({"username": "raj", "global_name": "R A J"}), "R A J"
        )
        self.assertEqual(discord_auth.display_name({"username": "raj"}), "raj")
        self.assertEqual(discord_auth.display_name({}), "Discord user")


class TheSignInRoutes(_DashboardFixture):
    """The redirect out, the callback back, and what the session then holds."""

    def _env(self):
        return mock.patch.dict(
            os.environ,
            {
                "DISCORD_CLIENT_ID": "1234567890",
                "DISCORD_CLIENT_SECRET": "shhh",
                "DASHBOARD_BASE_URL": "https://dashboard.test",
            },
        )

    def test_without_credentials_the_page_says_what_is_missing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            response = self.client.get("/login")
            page = response.get_data(as_text=True)
            self.assertIn("DISCORD_CLIENT_ID", page)
            self.assertIn("/login/discord/callback", page, "the callback URL to register")
            # The prose may still mention it as one of the ways in; what must be
            # absent is the button, because pressing it could only fail.
            self.assertNotIn('href="/login/discord"', page)

            refused = self.client.get("/login/discord")
            self.assertEqual(refused.status_code, 503)
            self.assertIn("DISCORD_CLIENT_ID", refused.get_data(as_text=True))

    def test_the_button_appears_when_it_can_work(self):
        with self._env():
            page = self.client.get("/login").get_data(as_text=True)
        self.assertIn("Sign in with Discord", page)
        self.assertIn('href="/login/discord"', page)

    def test_signing_in_sends_you_to_discord_with_a_state(self):
        with self._env():
            response = self.client.get("/login/discord")
        self.assertEqual(response.status_code, 302)
        location = response.headers["Location"]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        self.assertEqual(urllib.parse.urlparse(location).netloc, "discord.com")
        self.assertEqual(query["redirect_uri"], ["https://dashboard.test/login/discord/callback"])
        with self.client.session_transaction() as session:
            stored = session.get("oauth_state")
        self.assertTrue(stored, "no state was kept to compare the callback against")
        self.assertEqual(query["state"], [stored])

    def test_a_callback_that_did_not_come_from_here_is_refused(self):
        with self._env():
            self.client.get("/login/discord")  # establishes the real state
            wrong = self.client.get("/login/discord/callback?code=abc&state=guessed")
        self.assertEqual(wrong.status_code, 400)
        self.assertIn("did not come from this dashboard", wrong.get_data(as_text=True))
        with self.client.session_transaction() as session:
            self.assertIsNone(session.get("authenticated"))

    def test_cancelling_is_reported_without_being_treated_as_a_breach(self):
        with self._env():
            response = self.client.get(
                "/login/discord/callback?error=access_denied&error_description=The+user+denied"
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("not completed", response.get_data(as_text=True))

    def test_the_callback_signs_the_person_in_with_their_own_servers(self):
        admin_bit = str(discord_auth.MANAGE_GUILD)
        user = {"id": "777", "username": "raj", "global_name": "R A J", "avatar": None}
        guilds = [
            _guild(GUILD_ID, "Meow Server", owner=True),
            _guild(OTHER_GUILD_ID, "Other Server", permissions=admin_bit),
            _guild(424242, "Somebody else's", permissions="0"),
            _guild(555555, "Not in this bot", permissions=admin_bit),
        ]
        with self._env():
            self.client.get("/login/discord")
            with self.client.session_transaction() as session:
                state = session["oauth_state"]
            with mock.patch.object(discord_auth, "exchange_code", return_value="tok"), \
                 mock.patch.object(discord_auth, "fetch_user", return_value=user), \
                 mock.patch.object(discord_auth, "fetch_user_guilds", return_value=guilds):
                response = self.client.get(
                    f"/login/discord/callback?code=abc&state={state}"
                )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertTrue(session.get("authenticated"))
            self.assertEqual(session.get("auth_scope"), "user")
            self.assertEqual(session.get("user_id"), 777)
            self.assertEqual(session.get("discord_user", {}).get("name"), "R A J")
            ids = list(session.get("admin_guild_ids") or [])
        self.assertEqual(
            ids, [GUILD_ID, OTHER_GUILD_ID],
            "the list is the intersection of what they run and what the bot is in",
        )

    def test_the_session_holds_ids_not_a_copy_of_discords_state(self):
        """A cookie has a size limit, and a name per server is how a person who
        administers a few dozen servers gets signed out with no error anywhere."""
        admin_bit = str(discord_auth.MANAGE_GUILD)
        guilds = [_guild(GUILD_ID, "Meow Server", permissions=admin_bit)]
        user = {"id": "7", "username": "raj"}
        with self._env():
            self.client.get("/login/discord")
            with self.client.session_transaction() as session:
                state = session["oauth_state"]
            with mock.patch.object(discord_auth, "exchange_code", return_value="tok"), \
                 mock.patch.object(discord_auth, "fetch_user", return_value=user), \
                 mock.patch.object(discord_auth, "fetch_user_guilds", return_value=guilds):
                self.client.get(f"/login/discord/callback?code=a&state={state}")
        with self.client.session_transaction() as session:
            stored = dict(session)
        self.assertNotIn("admin_servers", stored)
        self.assertEqual(stored.get("admin_guild_ids"), [GUILD_ID])
        self.assertLess(len(repr(stored)), 1200, "the session is growing a cache of its own")

    def test_a_discord_session_cannot_reach_a_server_it_does_not_run(self):
        self.sign_in(guild_id=GUILD_ID, scope="user")
        with self.client.session_transaction() as session:
            session["admin_guild_ids"] = [GUILD_ID]
        # Ask for a server the bot is in but this person does not administer.
        page = self.api("GET", f"/dashboard?guild_id={OTHER_GUILD_ID}")
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn("Meow Server", body)
        self.assertNotIn("Other Server", body, "another server's page was served")

    def test_the_picker_refuses_a_form_naming_somebody_elses_server(self):
        self.sign_in(guild_id=GUILD_ID, scope="user")
        with self.client.session_transaction() as session:
            session["admin_guild_ids"] = [GUILD_ID]
        response = self.client.post(
            "/servers",
            data={"guild_id": str(OTHER_GUILD_ID), "csrf_token": CSRF},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("do not manage that server", response.get_data(as_text=True))
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("guild_id"), GUILD_ID, "the switch happened anyway")

    def test_the_picker_switches_within_the_list(self):
        self.sign_in(guild_id=GUILD_ID, scope="user")
        with self.client.session_transaction() as session:
            session["admin_guild_ids"] = [GUILD_ID, OTHER_GUILD_ID]
        response = self.client.post(
            "/servers",
            data={"guild_id": str(OTHER_GUILD_ID), "csrf_token": CSRF},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(str(OTHER_GUILD_ID), response.headers["Location"])
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("guild_id"), OTHER_GUILD_ID)

    def test_the_picker_lists_only_servers_the_bot_is_still_in(self):
        self.sign_in(guild_id=GUILD_ID, scope="user")
        with self.client.session_transaction() as session:
            session["admin_guild_ids"] = [GUILD_ID, 888888]
        body = self.client.get("/servers").get_data(as_text=True)
        self.assertIn("Meow Server", body)
        self.assertNotIn("Bot was removed", body)

    def test_the_picker_has_an_empty_state_that_offers_the_invite(self):
        self.sign_in(guild_id=GUILD_ID, scope="user")
        with self.client.session_transaction() as session:
            session["admin_guild_ids"] = []
        with self._env():
            body = self.client.get("/servers").get_data(as_text=True)
        # The invitation is what matters here, not the name in it: the page now
        # prints the live application's name, so a renamed bot says its own name
        # instead of a hardcoded one and this assertion cannot pin a string that
        # depends on the deployment.
        self.assertIn("to a server</a>", body)
        self.assertIn("oauth2/authorize", body)

    def test_a_one_time_link_has_nothing_to_pick(self):
        self.sign_in(guild_id=GUILD_ID, scope="guild")
        response = self.client.get("/servers")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard", response.headers["Location"])

    def test_the_operator_gets_every_server_with_what_each_one_is_running(self):
        """The passkey reaches every server, and this is how a second one is
        fixed without first knowing its id."""
        self.sign_in(guild_id=GUILD_ID, scope="global")
        body = self.client.get("/servers").get_data(as_text=True)
        self.assertIn("Meow Server", body)
        self.assertIn("Other Server", body, "every server the bot is in, not just the open one")
        self.assertIn("systems on", body, "the list should say what each server is running")
        self.assertIn("operator", body.lower())

    def test_the_servers_list_says_what_each_one_is_running(self):
        """Choosing which server to fix is otherwise a guess from a list of names."""
        self.sign_in(guild_id=GUILD_ID, scope="global")
        before = self.client.get("/servers").get_data(as_text=True)
        self.assertIn("systems on", before)
        self.assertNotIn("off:", before, "nothing is off yet")

        # Turn one system off the way the dashboard does, then ask again.
        import keep_alive as ka

        total = len(ka.AVAILABLE_MODULES)
        self.assertTrue(
            ka.modules.set_enabled(GUILD_ID, "market", False), "the switch did not save"
        )
        ka.modules.invalidate(GUILD_ID)
        after = self.client.get("/servers").get_data(as_text=True)
        self.assertIn(f"{total - 1} of {total} systems on", after)
        self.assertIn("off:", after)
        self.assertIn("Market", after, "the list should name what is off, not just count it")

    def test_the_operator_can_switch_to_any_server(self):
        self.sign_in(guild_id=GUILD_ID, scope="global")
        response = self.client.post(
            "/servers", data={"guild_id": str(OTHER_GUILD_ID), "csrf_token": CSRF}
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("guild_id"), OTHER_GUILD_ID)

    def test_the_picker_needs_a_session(self):
        response = self.client.get("/servers")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_the_picker_does_not_write_without_a_csrf_token(self):
        self.sign_in(guild_id=GUILD_ID, scope="user")
        with self.client.session_transaction() as session:
            session["admin_guild_ids"] = [OTHER_GUILD_ID]
        response = self.client.post("/servers", data={"guild_id": str(OTHER_GUILD_ID)})
        self.assertEqual(response.status_code, 403)
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("guild_id"), GUILD_ID)

    def test_the_dashboard_offers_the_invite_and_the_picker(self):
        self.sign_in(guild_id=GUILD_ID, scope="user")
        with self.client.session_transaction() as session:
            session["admin_guild_ids"] = [GUILD_ID, OTHER_GUILD_ID]
            session["discord_user"] = {"id": "777", "name": "R A J", "avatar": ""}
        with self._env():
            body = self.api("GET", "/dashboard").get_data(as_text=True)
        self.assertIn("Add to a server", body)
        self.assertIn("oauth2/authorize", body)
        self.assertIn("R A J", body, "the header does not say who is signed in")
        self.assertIn("/servers", body)
        self.assertIn("Other Server", body, "the switcher should offer their other server")

    def test_the_switcher_never_offers_a_server_outside_the_list(self):
        self.sign_in(guild_id=GUILD_ID, scope="user")
        with self.client.session_transaction() as session:
            session["admin_guild_ids"] = [GUILD_ID]
        with self._env():
            body = self.api("GET", "/dashboard").get_data(as_text=True)
        self.assertNotIn("Other Server", body)

    def test_a_refused_exchange_is_explained(self):
        with self._env():
            self.client.get("/login/discord")
            with self.client.session_transaction() as session:
                state = session["oauth_state"]
            with mock.patch.object(
                discord_auth, "exchange_code",
                side_effect=discord_auth.AuthError("Discord refused that (400: invalid_grant)."),
            ):
                response = self.client.get(f"/login/discord/callback?code=abc&state={state}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("invalid_grant", response.get_data(as_text=True))
        with self.client.session_transaction() as session:
            self.assertIsNone(session.get("authenticated"))

    def test_the_state_is_single_use(self):
        with self._env():
            self.client.get("/login/discord")
            with self.client.session_transaction() as session:
                state = session["oauth_state"]
            with mock.patch.object(
                discord_auth, "exchange_code",
                side_effect=discord_auth.AuthError("done"),
            ):
                first = self.client.get(f"/login/discord/callback?code=a&state={state}")
                second = self.client.get(f"/login/discord/callback?code=b&state={state}")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400, "the same state was accepted twice")


class TheRedirectUri(unittest.TestCase):
    """Which host Discord is told to send people back to."""

    def test_it_is_built_from_where_this_process_is(self):
        with mock.patch.dict(os.environ, {"DASHBOARD_BASE_URL": "https://meow.test/"}):
            self.assertEqual(
                keep_alive.oauth_redirect_uri(),
                "https://meow.test/login/discord/callback",
            )

    def test_a_hosted_deploy_uses_its_own_url(self):
        with mock.patch.dict(
            os.environ, {"RENDER_EXTERNAL_URL": "https://meow.onrender.com"}, clear=True
        ):
            self.assertEqual(
                keep_alive.oauth_redirect_uri(),
                "https://meow.onrender.com/login/discord/callback",
            )


class TheNextParameter(unittest.TestCase):
    """``next`` is a URL parameter, so it is a request and not a path."""

    def test_a_local_path_is_kept(self):
        self.assertEqual(keep_alive._safe_next("/dashboard?guild_id=5"), "/dashboard?guild_id=5")

    def test_another_site_is_dropped(self):
        for hostile in (
            "https://evil.example/steal",
            "//evil.example/steal",
            "javascript:alert(1)",
            "http://evil.example",
            "",
            None,
        ):
            self.assertIsNone(keep_alive._safe_next(hostile), hostile)

    def test_an_absurdly_long_path_is_cut(self):
        self.assertLessEqual(len(keep_alive._safe_next("/" + "a" * 5000)), 512)

    def test_a_tab_cannot_smuggle_another_host(self):
        """It arrived at the browser as ``//evil.example``.

        The tab is stripped when the redirect's ``Location`` header is built, so
        ``/\\t/evil.example`` left the string as ``//evil.example`` — a
        protocol-relative URL. The person had signed in on *this* domain and was
        handed straight to somebody else's, which is what a phishing link is.
        """
        for hostile in ("/\t/evil.example", "/\t//evil.example", "/\t\\evil.example"):
            self.assertIsNone(keep_alive._safe_next(hostile), hostile)

    def test_a_newline_is_not_a_path(self):
        """A newline cannot be written into a header at all.

        Werkzeug raises rather than emit it, so the callback answered **500**
        after a successful sign-in — the person authenticated and got an error
        page, with the session already replaced.
        """
        for hostile in ("/\n/evil.example", "/\r/evil.example", "/\x00/x", "/\x1b/x"):
            self.assertIsNone(keep_alive._safe_next(hostile), repr(hostile))

    def test_a_backslash_is_not_a_path(self):
        """Browsers read it as ``/`` for http(s), so it spells ``//``."""
        self.assertIsNone(keep_alive._safe_next("/\\evil.example"))

    def test_a_query_string_and_its_unescapes_still_come_through(self):
        """The refusals above are about control characters, not about punctuation."""
        for welcome in ("/dashboard", "/servers?guild_id=5", "/dashboard?a=b&c=d", "/a%20b/c"):
            self.assertEqual(keep_alive._safe_next(welcome), welcome)


class TheCallbackWithAHostileNext(_DashboardFixture):
    """``next`` reaches the redirect, so the hostile shapes are driven end to end.

    ``_safe_next`` returning ``None`` is the mechanism; what a person experiences
    is the redirect the callback actually emits, which is what these assert.
    """

    def _callback(self, next_value):
        with mock.patch.dict(
            os.environ,
            {
                "DISCORD_CLIENT_ID": "1234567890",
                "DISCORD_CLIENT_SECRET": "shhh",
                "DASHBOARD_BASE_URL": "https://dashboard.test",
            },
        ):
            self.client.get(f"/login/discord?next={next_value}")
            with self.client.session_transaction() as session:
                state = session["oauth_state"]
            with mock.patch.object(discord_auth, "exchange_code", return_value="tok"), \
                 mock.patch.object(discord_auth, "fetch_user", return_value={"id": "5", "username": "u"}), \
                 mock.patch.object(discord_auth, "fetch_user_guilds", return_value=[]):
                return self.client.get(f"/login/discord/callback?code=a&state={state}")

    def test_a_hostile_next_lands_on_this_site(self):
        for hostile in ("%2F%09%2Fevil.example", "%2F%0A%2Fevil.example", "%2F%5Cevil.example"):
            response = self._callback(hostile)
            self.assertEqual(response.status_code, 302, hostile)
            location = response.headers.get("Location") or ""
            self.assertFalse(location.startswith("//"), f"{hostile} left the site: {location}")
            self.assertIn(location, ("/dashboard", "/servers"), hostile)

    def test_a_welcome_next_is_still_followed(self):
        response = self._callback("%2Fdashboard%3Fguild_id%3D5")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers.get("Location"), "/dashboard?guild_id=5")


class TheLoginPageForTheOperator(unittest.TestCase):
    """The older ways in keep working, and the page describes all three."""

    def setUp(self):
        self.bot, _guild_obj, _other = make_bot()
        self._patched = mock.patch.object(keep_alive, "BOT_INSTANCE", self.bot)
        self._patched.start()
        self.client = keep_alive.app.test_client()

    def tearDown(self):
        self._patched.stop()

    def test_the_page_still_offers_the_passkey_and_the_link(self):
        # The passkey is read once at import, so it is the module constant that
        # has to be set — patching the environment at request time proves nothing.
        with mock.patch.object(keep_alive, "ADMIN_PASSKEY", "s3cret"):
            body = self.client.get("/login").get_data(as_text=True)
        self.assertIn("Admin Passkey", body)
        self.assertIn("Open Dashboard", body)
        self.assertIn('action="/login"', body, "the passkey form is gone")

    def test_the_front_page_sends_a_signed_in_session_to_the_dashboard(self):
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["auth_scope"] = "global"
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard", response.headers["Location"])


class InvitePermissionsCase(unittest.TestCase):
    """What the public invite link asks permission to do.

    It asked for `ADMINISTRATOR` — a blanket grant, published on the page whose
    whole job is to earn enough trust to be added. The argument for it was that
    one digit is easier to check than a bitfield, which is true and was still the
    wrong trade: it is the first thing a careful owner reads, and it makes every
    other permission irrelevant, so a fault in one command can do anything
    anywhere instead of being bounded by what that command needs.

    These tests decode the number with discord.py rather than comparing it to a
    literal, because a literal is exactly what drifted here: the *value* was
    asserted nowhere, so changing the grant was invisible.
    """

    def perms(self):
        import discord

        return discord.Permissions(discord_auth.INVITE_PERMISSIONS)

    def test_it_is_no_longer_a_blanket_administrator(self):
        self.assertFalse(
            self.perms().administrator,
            "the invite grants Administrator — that is the thing this replaced",
        )

    def test_every_named_permission_is_really_in_the_bitfield(self):
        perms = self.perms()
        for name, bit in discord_auth.INVITE_PERMISSION_BITS:
            self.assertTrue(
                hasattr(perms, name),
                f"{name} is not a permission discord.py knows — typo?",
            )
            self.assertTrue(
                getattr(perms, name),
                f"{name} ({bit}) is listed but is not set in the bitfield",
            )
            self.assertEqual(
                discord_auth.INVITE_PERMISSIONS & bit,
                bit,
                f"{name}: the bit and the name disagree",
            )

    def test_the_list_has_no_duplicate_or_conflicting_entries(self):
        names = [name for name, _bit in discord_auth.INVITE_PERMISSION_BITS]
        self.assertEqual(len(names), len(set(names)), "a permission is listed twice")
        bits = [bit for _name, bit in discord_auth.INVITE_PERMISSION_BITS]
        self.assertEqual(len(bits), len(set(bits)), "two names share one bit")
        # And the sum is a bitfield, so nothing may overlap by construction.
        self.assertEqual(
            discord_auth.INVITE_PERMISSIONS,
            sum(bits),
            "INVITE_PERMISSIONS is not the sum of the named bits",
        )

    def test_nothing_is_granted_that_no_call_site_needs(self):
        """The grants a hijacked token would reach for first.

        Each of these was checked against the tree before being left out: no
        code path pings `@everyone` (announcements ping a role, which needs no
        permission), music is gone and nothing joins a voice channel (the voice
        tracker reads state changes, which needs only `view_channel`), and
        nothing moves, mutes or deafens members or edits their nicknames — the
        only nickname the bot changes is its own, under `change_nickname`.
        """
        perms = self.perms()
        for name in (
            "administrator",
            "mention_everyone",
            "connect",
            "manage_guild",
            "manage_webhooks",
            "create_instant_invite",
            "move_members",
            "mute_members",
            "deafen_members",
            "manage_nicknames",
            "manage_threads",
        ):
            self.assertFalse(
                getattr(perms, name),
                f"the invite grants {name}, which nothing in the tree uses",
            )

    def test_the_invite_url_carries_that_number(self):
        """The list is only real if the link is built from it."""
        with mock.patch.dict(os.environ, {"DISCORD_CLIENT_ID": "1234567890"}):
            url = discord_auth.invite_url()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["permissions"], [str(discord_auth.INVITE_PERMISSIONS)])
        self.assertNotEqual(query["permissions"][0], "8")
        # The two scopes a working bot needs, unchanged by this.
        self.assertEqual(set(query["scope"][0].split()), set(discord_auth.INVITE_SCOPES))


if __name__ == "__main__":
    unittest.main()
