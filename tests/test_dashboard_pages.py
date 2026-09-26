"""The rebuilt dashboard's own contract, pinned against the pages as they ship.

The eleven addresses used to forward to `/servers`, and the composer and the
queue used to be driven by a page script talking to the JSON endpoints. Both
changed by request, and the two suites that pinned the scripted panel
(`tests/test_composer.py::DashboardTemplateCase` and
`tests/test_scheduler.py::ScheduleTemplateCase`) are retired with a reason and
point here.

What is pinned here is the *shape of a write* and the *ownership of a number*:

1. **Every control is a form, and it posts to the endpoint that owns the write.**
   No write needs JavaScript, and no rule about one is implemented twice.
2. **The numbers on the page come from the server's own constants**, read through
   the view — a template with a literal limit is a page that goes on stating the
   old one the day the constant moves.
3. **Names render as text.** A channel called `<script>` is a channel whose name
   is shown, not markup that runs.
4. **The ledgers are readings.** Nothing on the economy or levels page can write
   a balance; the switches they carry are the ones the Systems page carries.
"""
import re
import unittest

import keep_alive
from tests.test_composer import GUILD_ID as FIXTURE_GUILD
from utils import digest, scheduled

#: The server this suite's own fixture is aimed at. The composer fixture has its
#: own id, and a page test that assumed it would be pinning a number it does not
#: own — the rendered context is the only copy of that id worth asserting on.
GUILD_ID = 777002
CSRF = "csrf-token"
ROOT = "templates/dashboard.html"
#: The shape the registry hands the page — enough for a switch and a row, which
#: is all these tests are about.
MODULES = [
    {"id": "economy", "name": "Economy", "icon": "🪙", "category": "Play",
     "desc": "Coins, transfers and the bank.", "commands": ["balance", "pay"], "enabled": True},
    {"id": "games", "name": "Games", "icon": "🎲", "category": "Play",
     "desc": "The coin games.", "commands": ["roll"], "enabled": False},
    {"id": "leveling", "name": "Levels", "icon": "📈", "category": "Play",
     "desc": "XP, levels and the roles they grant.", "commands": ["rank"], "enabled": True},
    {"id": "fun", "name": "Fun", "icon": "🎉", "category": "Talk",
     "desc": "The small commands.", "commands": ["meow"], "enabled": True},
]


def flat(html: str) -> str:
    """The page with its line breaks collapsed.

    A sentence in a template is wrapped across lines, so a test that asserts a
    phrase has to read it as a reader does — one space wherever the source broke
    the line.
    """
    return re.sub(r"\s+", " ", html)


def roster_payload() -> dict:
    """A readable roster of one member, in the shape the reader returns."""
    return {
        "page": "members",
        "read": True,
        "why": "",
        "query": "",
        "roster": {
            "members": [
                {
                    "id": "9001",
                    "name": "raj",
                    "display_name": "raj",
                    "bot": False,
                    "avatar": "",
                    "joined_at": 1_700_000_000,
                    "joined_text": "2023-11-14 22:13 UTC",
                    "top_role": "Members",
                    "roles": [],
                    "timeout_seconds": 0,
                }
            ],
            "matched": 1, "shown": 1, "cache": 1, "queried": False, "complete": True,
        },
        "bans": [],
        "control": None,
        "abilities": None,
        "recent": [],
    }


def template() -> str:
    """The shipped template, as source. Shape questions are asked here."""
    import pathlib

    return pathlib.Path(ROOT).read_text(encoding="utf-8")


class _PageRender(unittest.TestCase):
    """The template, rendered with the context the view builds.

    Imported from ``tests.test_composer`` rather than rebuilt: one fixture for one
    template, and the point of these tests is that the *page* is right, not that
    the fixture is.
    """

    @staticmethod
    def render(**overrides):
        from tests.test_composer import render_dashboard

        return render_dashboard(**overrides)

    def page(self, name: str, **overrides) -> str:
        overrides.setdefault("dash_page", name)
        return self.render(**overrides)


class TheComposerIsAForm(_PageRender):
    """The composer's controls, its wiring, and where its numbers come from."""

    def test_the_composer_controls_are_on_the_page(self):
        html = self.page("composer")
        for element_id in (
            "composer-channels",
            "composer-title",
            "composer-body",
            "composer-mentions",
            "composer-embed",
        ):
            self.assertIn(f'id="{element_id}"', html, element_id)

    def test_the_form_posts_to_the_endpoint_that_owns_the_write(self):
        html = self.page("composer")
        self.assertIn('action="/api/send-message"', html)
        self.assertIn('method="post"', html)

    def test_the_form_carries_the_server_the_token_and_the_way_back(self):
        """One hidden field per value every write needs, from one macro."""
        html = self.page("composer")
        for name in ("csrf_token", "guild_id", "dash_return", "dash_notice"):
            self.assertIn(f'name="{name}"', html, name)
        self.assertIn(f'value="{CSRF}"', html)
        self.assertIn(f'value="{FIXTURE_GUILD}"', html)

    def test_the_limits_come_from_the_servers_own_constants(self):
        """A different rulebook reaches the page — so it is read, not restated."""
        html = self.page(
            "composer",
            composer_limits={"chars": 321, "tags": 4, "channels": 2, "title": 77},
        )
        self.assertIn('maxlength="321"', html)
        self.assertIn('maxlength="77"', html)
        self.assertIn("4 at most", html)
        self.assertIn("2 at most", html)
        self.assertNotIn(str(keep_alive.MESSAGE_MAX_CHARS), html)

    def test_the_page_ships_no_inline_script_of_its_own(self):
        """Nothing to parse, nothing to fail silently: the composer is a form."""
        scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", template())
        self.assertEqual(scripts, [], "the dashboard carries an inline script again")

    def test_the_page_ships_no_inline_style_and_links_the_shared_sheets(self):
        """The page is a page of this site: the shell's own layout sheet is gone.

        It used to be a standalone document with a 1,660-line `<style>` block and
        an asset handed to it alone (`static/dashboard.css`). What it wears now is
        the shell everybody wears, so a change to a card is a change to the page
        sheet rather than to one page.
        """
        html = self.page("overview")
        self.assertNotIn("<style", html)
        self.assertIn("/static/meow.css", html)
        self.assertIn("/static/pages.css", html)
        self.assertNotIn("dashboard.css", html)

    def test_every_form_action_is_an_address_this_app_serves(self):
        """A form that posts nowhere is a dead end with a button on it."""
        served = {rule.rule for rule in keep_alive.app.url_map.iter_rules()}
        actions = set(re.findall(r'action="(/[^"]*)"', template()))
        self.assertTrue(actions, "no forms found — this test would pass vacuously")
        for action in actions:
            with self.subTest(action=action):
                # The queue's two forms name a message id, so the address is a
                # template expression rather than a literal — it is checked as
                # the rule it renders to, and a test only ever sees the source.
                if "{{" in action:
                    self.assertRegex(
                        action,
                        r"^/api/scheduled/\{\{ message\.id \}\}/(cancel|fire)$",
                        "a template's form action is not one this app serves",
                    )
                    continue
                self.assertIn(action, served, "a form posts to an address no route serves")


#: What each action-taking endpoint will accept, read from the app rather than
#: typed here — the pair this law exists to keep together.
ACCEPTED_ACTIONS = {
    "/api/server-action": keep_alive.SERVER_ACTIONS,
    "/api/member-action": keep_alive.PANEL_MEMBER_ACTIONS,
}


def named_actions(form: str) -> list[str]:
    """Every value a form can send as its ``action`` — hidden field or option."""
    values = re.findall(r'name="action"[^>]*value="([^"]+)"', form)
    for select in re.findall(r'<select\b[^>]*name="action".*?</select>', form, re.S):
        values += re.findall(r'<option[^>]*value="([^"]+)"', select)
    return values


class EveryActionAFormNamesIsOneTheEndpointAccepts(_PageRender):
    """The two halves of one write, held to each other instead of to a list.

    A page and an endpoint can each be right and still disagree: the Commands
    page shipped a button posting ``action=resync`` while the endpoint compared
    that action against the tuple a *server's* page draws its three moderation
    controls from, so the one control offered for a stale command picker
    answered **Unknown action** and nothing else. The test below that walked
    ``PANEL_SERVER_ACTIONS`` could not see it — it asked the list whether the
    list was complete. This reads what the page actually posts.
    """

    def test_every_named_action_is_accepted_by_the_endpoint_it_posts_to(self):
        checked = 0
        for form in re.findall(r"<form\b.*?</form>", template(), re.S):
            target = re.search(r'action="([^"]+)"', form)
            if not target or target.group(1) not in ACCEPTED_ACTIONS:
                continue
            accepted = ACCEPTED_ACTIONS[target.group(1)]
            for value in named_actions(form):
                if "{{" in value:
                    # Options drawn from a tuple in the context rather than typed
                    # into the template: `test_the_member_and_server_actions_post
                    # _to_their_endpoints` above walks that tuple through the
                    # *rendered* page, so each option is already held to the
                    # endpoint. What this test is for is the other shape — a
                    # hand-written `value="…"`, which nothing was comparing.
                    drawn = re.search(r"\{%\s*for action in (\w+)\s*%\}", form)
                    self.assertIn(
                        drawn.group(1) if drawn else "",
                        ("member_actions", "server_actions"),
                        f"{target.group(1)} draws its actions from somewhere unguarded",
                    )
                    continue
                checked += 1
                self.assertIn(
                    value,
                    accepted,
                    f"a form posts action={value!r} to {target.group(1)}, which refuses it",
                )
        self.assertGreater(checked, 0, "no action-naming form was read — this test is vacuous")

    def test_the_console_only_actions_are_not_offered_on_a_server_page(self):
        """The other direction, which matters more: a door must not widen by accident.

        ``leave`` and ``block`` make the bot abandon a server and a server's own
        administrator must not be able to ask for that from their own dashboard.
        """
        for value in ("leave", "block", "unblock"):
            self.assertNotIn(f'value="{value}"', template(), value)


class TheWritesAreTheEndpoints(_PageRender):
    """Each page's write goes to the endpoint that already owned it."""

    def test_the_systems_switch_posts_to_the_toggle_endpoint(self):
        html = self.page("systems", modules=MODULES, active_count=3, total_count=4)
        self.assertIn('action="/api/toggle"', html)
        self.assertIn('data-module=', html)
        # A module that is *on* is switched off by the absence of `enabled`: an
        # empty string would read as false and a "0" would read as true.
        self.assertIn('role="switch"', html)

    def test_the_bulk_buttons_post_to_the_bulk_endpoint(self):
        html = self.page("systems", modules=MODULES, active_count=3, total_count=4)
        self.assertIn('action="/api/toggle-all"', html)
        self.assertIn('name="enabled" value="1"', html)

    def test_the_member_and_server_actions_post_to_their_endpoints(self):
        members = self.page("members", management=roster_payload())
        self.assertIn('action="/api/member-action"', members)
        self.assertIn('name="user_id"', members)
        for action in keep_alive.PANEL_MEMBER_ACTIONS:
            self.assertIn(f'value="{action}"', members, action)
        server = self.page("server")
        self.assertIn('action="/api/server-action"', server)
        for action in keep_alive.PANEL_SERVER_ACTIONS:
            self.assertIn(f'value="{action}"', server, action)

    def test_the_identity_form_is_multipart_and_names_the_endpoints_fields(self):
        html = self.page("identity")
        self.assertIn('action="/api/identity"', html)
        self.assertIn('enctype="multipart/form-data"', html)
        for field in ("nick", "bio", "clear_avatar", "clear_banner"):
            self.assertIn(f'name="{field}"', html, field)

    def test_the_queue_rows_carry_their_own_two_actions(self):
        html = self.page(
            "schedule",
            schedule_queue={
                "read": True,
                "pending": 1,
                "max_pending": 10,
                "catchup_hours": 6,
                "upcoming": [
                    {
                        "id": 41,
                        "run_at": 1_700_000_000,
                        "run_at_text": "2023-11-14 22:13 UTC",
                        "channels": [{"id": "1", "name": "general", "kind": "text", "exists": True}],
                        "generator_label": "",
                        "preview": "a queued announcement",
                        "content": "a queued announcement",
                        "created_by_name": "raj",
                        "results": [],
                    }
                ],
                "recent": [],
            },
        )
        self.assertIn('action="/api/scheduled/41/fire"', html)
        self.assertIn('action="/api/scheduled/41/cancel"', html)
        self.assertIn("a queued announcement", html)

    def test_a_queue_that_could_not_be_read_says_so(self):
        """Not \"nothing is queued\" — the one reading that is worse than a blank."""
        html = self.page(
            "schedule",
            schedule_queue={"read": False, "upcoming": [], "recent": [],
                            "pending": 0, "max_pending": 0, "catchup_hours": 0},
        )
        self.assertIn("could not be read", html)
        self.assertNotIn("Nothing is queued here", html)


class TheSchedulingControls(_PageRender):
    """The queue's form, against the store and the registry it must agree with."""

    def test_the_time_field_is_a_real_datetime_field_labelled_utc(self):
        html = self.page("schedule")
        self.assertIn('type="datetime-local"', html)
        self.assertIn('name="run_at_utc"', html)
        self.assertIn("UTC", html, "a form cannot know the reader's zone, so it must say")
        self.assertIn(str(1_700_000_000), html, "the server's own clock is not shown")

    def test_the_repeat_list_is_the_stores_own(self):
        html = self.page("schedule")
        for minutes, label in scheduled.REPEAT_CHOICES:
            self.assertIn(f'value="{minutes}"', html, label)
            self.assertIn(f">{label}<", html, label)

    def test_the_content_sources_are_the_generators_that_exist(self):
        html = self.page("schedule")
        self.assertIn("Message I write", html)
        self.assertTrue(digest.GENERATORS, "this test would pass vacuously")
        for name, entry in digest.GENERATORS.items():
            with self.subTest(generator=name):
                self.assertIn(f'value="{name}"', html)
                self.assertIn(entry["label"], html)

    def test_a_built_message_says_its_text_does_not_exist_yet(self):
        html = flat(self.page("schedule"))
        self.assertIn("does not exist until the moment it posts", html)

    def test_the_queue_outliving_the_tab_is_stated(self):
        html = flat(self.page("schedule"))
        self.assertIn("outlives this tab", html)
        self.assertIn("sent now or on a schedule", html)


class ThePublishLineIsServerRendered(_PageRender):
    """Discord's own answer, stated by the server, before any script of the page's.

    Where this moved *from*: `tests/test_command_publish.py` pinned the old
    scripted panel — a tone→dot vocabulary, a four-second sleep, a stamp handed to
    `scheduleScopeRefresh` so the page could wait for its own republish to land.
    That panel is gone with the dashboard's inline script (the host's CSP carries
    no `unsafe-inline`), so what is left to pin is the part that was always the
    real contract: the page states the reading, whole, from its one author.

    The scoping — that a page with no server is not Discord's *global* lane — is
    pinned against the route in `tests/test_command_publish.py`, where the bug it
    describes could actually be seen.
    """

    def test_the_sentence_and_its_tone_arrive_with_the_render(self):
        html = self.page(
            "commands",
            command_scope_publish={
                "tone": "ok", "held": 90, "pending": 0, "matches": True,
                "at": 1_700_000_000, "age_seconds": 2,
                "text": (
                    "Discord holds 90 commands, matching what this server's switches "
                    "say. Checked 2 seconds ago."
                ),
            },
        )
        self.assertIn('id="scope-publish"', html)
        self.assertIn('data-tone="ok"', html)
        self.assertIn('id="scope-publish-text"', html)
        self.assertIn("Discord holds 90 commands", html)
        # The one-author half of this — the page must hand the sentence over
        # whole rather than assemble it — is asserted on the template's *source*
        # in `tests/test_command_publish.py`, where a rendered string cannot
        # answer it. Here the question is only whether the reading is on the page.

    def test_a_scope_nobody_has_read_does_not_read_as_a_match(self):
        html = self.page("commands", command_scope_publish={
            "tone": "never", "held": None, "pending": 0, "matches": None,
            "at": None, "age_seconds": None,
            "text": (
                "Discord's own list has not been read since this process started. "
                "The counts above are what this server's switches say, not what "
                "Discord holds."
            ),
        })
        self.assertIn('data-tone="never"', html)
        self.assertIn("has not been read since this process started", html)
        self.assertNotIn('data-tone="ok"', html)

    def test_a_disagreement_is_drawn_as_a_disagreement(self):
        """The one case this reading exists for: a count that is not the promised one."""
        html = self.page("commands", command_scope_publish={
            "tone": "ok", "held": 88, "pending": 0, "matches": False,
            "at": 1_700_000_000, "age_seconds": 2,
            "text": (
                "Discord holds 88 commands — 2 fewer than this server's switches say "
                "it should. Checked 2 seconds ago."
            ),
        })
        self.assertIn("Disagrees with the switches", html)
        # Apostrophe-free on purpose: the page escapes `server's` to `&#39;`, and a
        # test that asserts the unescaped form is asserting against autoescaping
        # rather than against the page.
        self.assertIn("2 fewer than this server", html)

    def test_the_readers_own_keys_are_not_printed_as_headings(self):
        """A mapping dumped as facts prints `age seconds` and `at` under a heading.

        The page did exactly that in its first draft: `facts()` walks a mapping's
        scalars, so the panel showed its reader's own field names beside the
        sentence that already said all of it in words.
        """
        html = flat(self.page("commands"))
        for internals in ("age seconds", "age_seconds", "<dt>matches", "<dt>at<"):
            with self.subTest(internals=internals):
                self.assertNotIn(internals, html, "the panel is printing its reader")


class NamesAreText(_PageRender):
    """Nothing a server can name may become markup on the page."""

    def test_a_channel_named_with_markup_is_shown_not_run(self):
        html = self.page(
            "composer",
            channel_choices=[
                {"id": "1", "name": "<script>alert(1)</script>", "kind": "text", "can_send": True}
            ],
        )
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_a_member_named_with_markup_is_shown_not_run(self):
        management = roster_payload()
        member = management["roster"]["members"][0]
        member.update(
            {"name": "<img src=x onerror=alert(1)>", "display_name": "<b>bold</b>",
             "top_role": "<i>role</i>"}
        )
        html = self.page("members", management=management)
        self.assertNotIn("<img src=x onerror=alert(1)>", html)
        self.assertNotIn("<b>bold</b>", html)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", html)


class TheLedgerPagesAreReadings(_PageRender):
    """Both ledgers draw their own section, and neither offers to write a balance."""

    def test_both_pages_draw_a_labelled_section(self):
        for page, anchor in (("economy", "economy"), ("leveling", "leveling")):
            with self.subTest(page=page):
                html = self.page(page)
                self.assertIn(f'aria-labelledby="{page}-title"', html)
                self.assertIn(f'<h2 id="{page}-title">', html)

    def test_the_economy_page_prints_the_ledger_and_the_fee(self):
        html = self.page(
            "economy",
            economy_display={
                "wallet": "1,550", "bank": "4,100", "circulation": "5,650",
                "meow_bank": "250", "accounts": 2,
                "top": [{"user_id": 1, "name": "raj", "net_text": "5,250",
                         "wallet_text": "1,250", "bank_text": "4,000", "share_text": "92.9%"}],
            },
            economy_fee={"label": "2%", "enabled": True, "cap": 500,
                         "stored": True, "describe": "2% of a transfer goes to DaCat Bank."},
        )
        self.assertIn("In wallets", html)
        self.assertIn("1,550", html)
        self.assertIn("4,100", html)
        self.assertIn("DaCat Bank", html)
        self.assertIn("2%", html)
        self.assertIn("raj", html)
        self.assertIn("5,250", html)

    def test_an_empty_economy_says_nothing_has_been_earned(self):
        html = self.page(
            "economy",
            economy_display={"wallet": "0", "bank": "0", "circulation": "0",
                             "meow_bank": "0", "accounts": 0, "top": []},
        )
        self.assertIn("Nobody has earned a coin here yet", html)

    def test_the_levels_page_prints_the_stored_rates_and_a_repaired_row(self):
        html = self.page(
            "leveling",
            level_display={
                "text_xp": 18, "coin_band": "7–21", "voice": "5 XP · 4 🪙",
                "stored": True, "channel_id": 0, "dm": False,
                "top": [{"user_id": 1, "name": "raj", "level": 12, "level_text": "Level 12",
                         "repaired": True, "progress_text": "1,200 / 2,400 XP",
                         "xp_text": "17,190", "messages": 400, "voice_minutes": 120}],
            },
            level_view={"members": 1, "xp_total": 17190, "highest": 12, "messages": 400,
                        "voice_minutes": 120, "average_level": 12.0, "roles": []},
        )
        self.assertIn("18 XP", html)
        self.assertIn("7–21 🪙", html)
        self.assertIn("5 XP · 4 🪙", html)
        self.assertIn("as configured here", html)
        self.assertIn("Level 12", html)
        self.assertIn("repaired from XP", html)

    def test_an_empty_levels_page_says_which_reading_it_holds(self):
        html = self.page(
            "leveling",
            level_display={"text_xp": 15, "coin_band": "4–10", "voice": "3 XP · 2 🪙",
                           "stored": False, "channel_id": 0, "dm": False, "top": []},
            level_view={"members": 0, "xp_total": 0, "highest": 0, "messages": 0,
                        "voice_minutes": 0, "average_level": 0.0, "roles": []},
        )
        self.assertIn("Nobody has earned a level here yet", html)
        self.assertIn("No level roles on this server", html)

    def test_the_two_pages_carry_the_switches_they_are_about(self):
        economy = self.page("economy", modules=MODULES, active_count=3, total_count=4)
        self.assertIn('data-module="economy"', economy)
        self.assertIn('data-module="games"', economy)
        levels = self.page("leveling", modules=MODULES, active_count=3, total_count=4)
        self.assertIn('data-module="leveling"', levels)

    def test_neither_ledger_offers_to_write_a_balance(self):
        for page in ("economy", "leveling"):
            with self.subTest(page=page):
                html = self.page(page)
                self.assertNotIn("member-action", html)
                self.assertNotIn('name="amount"', html)
                self.assertNotIn("coins to give", html.lower())


class TheOverviewReadsTheServer(_PageRender):
    """The front page answers a question about the server, not about the process."""

    def test_a_reading_that_found_nothing_says_so_rather_than_zero(self):
        html = self.page(
            "overview",
            overview_live={"read": True, "why": "", "rooms": [], "people": 0,
                           "alone": 0, "radio": None},
        )
        self.assertIn("In voice right now", html)
        self.assertIn("Nobody is in a voice channel. That is the reading, not a failure.", html)

    def test_a_reading_that_could_not_be_taken_says_that_instead(self):
        html = self.page(
            "overview",
            overview_live={"read": False, "why": "the gateway is asleep",
                           "rooms": [], "people": 0, "alone": 0, "radio": None},
        )
        self.assertIn("Nothing was read for this render.", html)
        self.assertIn("the gateway is asleep", html)
        self.assertNotIn("Nobody is in a voice channel", html)

    def test_the_overview_offers_the_four_destinations_with_this_server(self):
        html = self.page(
            "overview",
            dashboard_pages=keep_alive._page_nav(
                keep_alive.DASHBOARD_PAGES, "overview", f"?guild_id={GUILD_ID}"
            ),
        )
        for path in ("members", "server", "composer", "schedule"):
            self.assertIn(f'href="/dashboard/{path}?guild_id={GUILD_ID}"', html, path)


if __name__ == "__main__":
    unittest.main()
