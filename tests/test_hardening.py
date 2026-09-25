"""Regression tests for the audit fixes that are not money arithmetic.

Each one pins a concrete defect found in a full pass over the live tree: a
published default dashboard credential, moderation case numbers that disagreed
with the stored record, deferred interactions left unanswered, a listener killed
by an unguarded delete, two read-modify-write races, a starboard row that could
never recover, and commands crashing in DMs.
"""
import asyncio
import unittest
from pathlib import Path
from unittest import mock

import keep_alive
from utils import db, features

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class NoPublishedCredential(unittest.TestCase):
    """A hardcoded passkey is a credential in a public repository."""

    def test_the_old_default_is_gone_everywhere(self):
        for rel in ("keep_alive.py", "README.md", "HANDOFF.md", ".env.example", "CHANGELOG.md"):
            path = ROOT / rel
            if path.exists():
                self.assertNotIn("Him@2009@Meoww", path.read_text(encoding="utf-8"), rel)

    def test_passkey_has_no_fallback_value(self):
        source = _read("keep_alive.py")
        # The environment value is used verbatim, through the one module that
        # reads the environment, and there is no `or "<something>"` fallback —
        # so an unset variable yields an empty (unusable) passkey rather than a
        # compiled-in credential. This used to match the literal
        # `os.environ.get(...)` line; the reader replaced that line, and the
        # property being checked is the same one, so the check moved with it.
        self.assertIn('ADMIN_PASSKEY = config_audit.get("ADMIN_PASSKEY")', source)
        self.assertNotRegex(source, r'ADMIN_PASSKEY\s*=\s*[^\n(]*or\s+"[^"]+"')

    def test_passkey_endpoints_reject_an_empty_credential(self):
        source = _read("keep_alive.py")
        # Passkey APIs go through _passkey_ok, which refuses an empty/missing
        # ADMIN_PASSKEY and never reads the secret from the query string.
        self.assertIn("def _passkey_ok", source)
        self.assertIn("if not ADMIN_PASSKEY:", source)
        self.assertGreaterEqual(source.count("_passkey_ok()"), 3)
        self.assertNotIn('request.args.get("passkey")', source)


class ModCaseNumbers(unittest.TestCase):
    """The number shown to users must be the row's real id."""

    def test_case_number_is_the_inserted_row_id(self):
        import os
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp(prefix="meow-case-")
        original = db.DB_PATH
        try:
            db.DB_PATH = os.path.join(tmp, "cases.db")
            conn = db.connect()
            conn.execute("""CREATE TABLE mod_cases (
                case_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, case_type TEXT,
                user_id INTEGER, moderator_id INTEGER, reason TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
            conn.commit()
            conn.close()

            first = asyncio.run(db.add_mod_case(1, "Warn", 10, 99, "first"))
            second = asyncio.run(db.add_mod_case(1, "Kick", 11, 99, "second"))

            self.assertEqual(first, 1)
            self.assertEqual(second, 2)

            # Deleting a row must not renumber later cases — the old COUNT(*)+1
            # counter would have handed out a number that already existed.
            conn = db.connect()
            conn.execute("DELETE FROM mod_cases WHERE case_id = ?", (first,))
            conn.commit()
            conn.close()

            third = asyncio.run(db.add_mod_case(1, "Ban", 12, 99, "third"))
            self.assertEqual(third, 3)

            conn = db.connect()
            stored = {row[0] for row in conn.execute("SELECT case_id FROM mod_cases")}
            conn.close()
            self.assertIn(third, stored, "the reported case number must exist in the table")
        finally:
            db.DB_PATH = original
            shutil.rmtree(tmp, ignore_errors=True)


class DeferredErrorsAreAnswered(unittest.TestCase):
    def test_error_handler_falls_back_to_a_followup(self):
        source = _read("bot.py")
        self.assertIn("if interaction.response.is_done():", source)
        self.assertIn("await interaction.followup.send(message, ephemeral=True)", source)


class ListenerHardening(unittest.TestCase):
    def test_automod_delete_is_guarded(self):
        source = _read("cogs/automod.py")
        self.assertIn("except (discord.Forbidden, discord.NotFound, discord.HTTPException)", source)

    def test_vc_reward_write_is_atomic(self):
        source = _read("cogs/leveling.py")
        # The per-minute payout is passed in as arguments, so the signature grew
        # with the reward maths; what matters is that the read-decide-write still
        # runs inside one ``db.run_atomic`` transaction.
        self.assertIn("def _vc_tick(cur, xp_gain=", source)
        self.assertIn("await asyncio.to_thread(db.run_atomic, _vc_tick)", source)

    def test_chat_cooldown_map_is_pruned(self):
        source = _read("cogs/leveling.py")
        self.assertIn("if len(self._chat_cooldowns) > 4096:", source)

    def test_starboard_recovers_when_its_card_was_deleted(self):
        source = _read("cogs/starboard.py")
        self.assertIn("except discord.NotFound:", source)
        self.assertIn("INSERT OR REPLACE INTO starboard_posts", source)


class DmGuard(unittest.TestCase):
    def test_dm_allow_list_only_names_real_commands(self):
        """A typo would silently make a command unusable in DMs."""
        import re

        commands = set()
        for path in list((ROOT / "cogs").glob("*.py")) + [ROOT / "bot.py"]:
            source = path.read_text(encoding="utf-8")
            commands.update(re.findall(r'@app_commands\.command\(name="([^"]+)"', source))
            commands.update(re.findall(r'@bot\.tree\.command\(name="([^"]+)"', source))
            # A command inside a group is declared on the group (`@legal_group
            # .command(...)`), and what the DM guard compares against for it is the
            # *group's* name — so both halves have to be collected here.
            commands.update(re.findall(r'@\w+\.command\(name="([^"]+)"', source))
            commands.update(re.findall(r'app_commands\.Group\(\s*(?:\n\s*)?name="([^"]+)"', source))
        unknown = sorted(features.DM_ALLOWED_COMMANDS - commands)
        self.assertEqual(unknown, [], f"DM allow-list names non-existent commands: {unknown}")

    def test_bot_enforces_the_dm_guard(self):
        source = _read("bot.py")
        self.assertIn("features.DM_ALLOWED_COMMANDS", source)
        self.assertIn("only works inside a server", source)

    def test_member_cache_helper_defaults_to_false(self):
        from utils import style

        self.assertFalse(style.member_cache_complete(object()))


class GrowingTablesAreIndexed(unittest.TestCase):
    """A missing index is invisible until the table is big, and then it is the query.

    The composite primary keys cover economy, user_levels, item_effects, loans and
    market_holdings, but the chart series, the trade history, /inventory, /quote and
    the warning list all filter on columns that had no index at all.
    """

    def test_the_access_paths_have_indexes(self):
        import os
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp(prefix="meow-idx-")
        original = db.DB_PATH
        try:
            db.DB_PATH = os.path.join(tmp, "indexes.db")
            asyncio.run(db.setup())
            conn = db.connect()
            try:
                names = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                }
            finally:
                conn.close()
        finally:
            db.DB_PATH = original
            shutil.rmtree(tmp, ignore_errors=True)

        for index in (
            "idx_market_history_symbol_ts",
            "idx_market_trades_owner",
            "idx_inventory_owner",
            "idx_custom_quotes_guild",
            "idx_warnings_owner",
        ):
            self.assertIn(index, names, f"{index} is missing, so its query scans")


class TheAuditFixesAreInTheShippedPages(unittest.TestCase):
    def test_the_market_page_announces_its_state(self):
        """Its status line is the only place "trading paused" is ever said.

        Asked of the three files the page is made of, one question each, because
        the rebuild moved them apart: the markup carries the live regions, the
        sheet carries the focus ring (it left the template when the market's
        colour did), and the script is the one place a price becomes DOM — which
        is exactly where markup could be built out of somebody's quote.

        The last two assertions are the pair that matters: the script has to
        write text *and* not write markup, and a guard that only checked the
        second would pass on a page that had stopped rendering prices at all.
        """
        html = _read("templates/market.html")
        self.assertIn('role="status"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn('aria-pressed=', html)
        self.assertIn('role="img"', html)
        self.assertIn("Trading pauses", html, "the one place a refusal is explained")
        self.assertIn(
            ":focus-visible", _read("static/market.css"),
            "keyboard users need to see where they are",
        )
        import re

        script = _read("static/market.js")
        self.assertIn("textContent", script, "the prices are written, not formatted into markup")
        assigned = {value.strip() for value in re.findall(r"innerHTML\s*=\s*([^;]+);", script)}
        self.assertEqual(
            assigned, {'""'},
            "innerHTML is only ever used to empty a container, never to fill one",
        )

    def test_the_composer_does_not_need_a_script_to_hold_a_message(self):
        """Retired, and the claim it was written for is now the stronger one.

        This pinned a `sessionStorage` draft — a key written on every keystroke
        and restored after an expired sign-in — as the answer to "a long message
        must not be lost when the tab is sent back to the door". The rebuild
        answered the same risk by removing the mechanism the loss could happen
        in: the composer is a `<form method="post">` whose fields are the
        browser's own, and there is **no page script at all** in
        `templates/dashboard.html` (zero `<script>` elements, asserted below),
        so there is nothing to keep in a draft store and nothing that can forget
        to save one. `tests/test_dashboard.py` pins the form, its fields and its
        endpoint; `tests/test_design_laws.py::FieldsAreNamed` pins that every
        field is labelled.
        """
        html = _read("templates/dashboard.html")
        self.assertNotIn(
            "<script",
            html,
            "a script is back on the dashboard, so this law needs a subject "
            "again: a write that needs one is a write that is unavailable "
            "exactly when something is wrong",
        )


class ThePagesStayReadable(unittest.TestCase):
    """Small text on these dark cards must clear the 4.5:1 AA floor.

    Two colours were below it — ``#64748b``, which carried 22 pieces of small
    text (counters, notes, timestamps) at 3.7:1, and the accent colour used *as
    text* at 3.9:1 while it is fine behind white on a filled button. Neither is
    visible as a defect to whoever reintroduces one, so the ratio is checked.
    """

    #: Every surface these pages paint text on, lightest last (the worst case).
    #:
    #: Two palettes, because two are live: the pre-rebuild grounds are what the
    #: market, the sign-in doors and the per-server dashboard still paint, and
    #: `#0a0a0d`/`#131318`/`#0b0b0f` are the rebuilt frame's — so a colour has to
    #: clear the floor on all six rather than on whichever set the last rebuild
    #: left behind. The worst of the six is what is reported.
    SURFACES = (
        "#090c10", "#131821", "#0e131b",
        "#0a0a0d", "#131318", "#0b0b0f",
    )
    #: The pages, and the shared stylesheet they all link.
    #:
    #: ``static/meow.css`` is in this list because it is where most of the site's
    #: colour now lives: the sign-in and picker pages and the landing page hold no
    #: CSS of their own at all, so a guard that only read the templates would have
    #: gone on passing while checking almost nothing.
    PAGES = (
        "templates/dashboard.html",
        "templates/market.html",
        "templates/login.html",
        "static/meow.css",
        # The frame the rebuilt pages are drawn in, and the sheet that now holds
        # most of the site's colour — the front door, `/systems`, `/start`, the
        # radio page and the policies hold no palette of their own at all, so a
        # guard that read only their templates would go on passing while
        # checking almost nothing. This is `static/dashboard.css`'s slot, one
        # rebuild later.
        "static/meoww.css",
        # The interiors' rules moved out of the dashboard's template into this
        # sheet, which is the second half of the same pair: `meoww.css` for the
        # frame, `pages.css` for what sits inside it.
        "static/pages.css",
        # The market moved its own out the same way, and for the same reason.
        "static/market.css",
    )
    FLOOR = 4.5

    @staticmethod
    def _channel(value: float) -> float:
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    @classmethod
    def _luminance(cls, color: str) -> float:
        raw = color.lstrip("#")
        if len(raw) == 3:
            raw = "".join(ch * 2 for ch in raw)
        r, g, b = (int(raw[i:i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * cls._channel(r) + 0.7152 * cls._channel(g) + 0.0722 * cls._channel(b)

    @classmethod
    def _ratio(cls, fg: str, bg: str) -> float:
        a, b = cls._luminance(fg), cls._luminance(bg)
        return (max(a, b) + 0.05) / (min(a, b) + 0.05)

    @classmethod
    def _unreadable_text(cls, source: str) -> list[tuple[str, float]]:
        """Text colours in a template that fail the floor on every surface.

        Rules that declare their own ``background`` are skipped: white on a
        filled accent button is measured against that fill, not the page.
        """
        import re

        # A template carries its CSS in ``<style>`` blocks; a stylesheet *is* the
        # style, and has none.
        blocks = re.findall(r"<style>(.*?)</style>", source, re.S)
        style = "\n".join(blocks) if blocks else source
        root = re.search(r":root\s*\{(.*?)\}", style, re.S)
        variables = dict(
            re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,6})", root.group(1) if root else "")
        )
        bad = []
        for rule in re.findall(r"[^{}]+\{[^{}]*\}", style):
            if "background" in rule:
                continue
            for value in re.findall(r"(?<![-\w])color:\s*(var\(--[\w-]+\)|#[0-9a-fA-F]{3,6})", rule):
                color = variables.get(value[4:-1]) if value.startswith("var(") else value
                # The two page grounds, and nothing else: `--accent-ink` is the
                # ink that goes *on* a filled control, and it is deliberately the
                # page's own black (`--text-invert` is the light palette's
                # white) — measured against the page it is 1:1 by construction,
                # which is a fact about the pairing rather than about the ink.
                if not color or color.lower() in ("#090c10", "#090c11", "#0a0a0d"):
                    continue  # unresolvable, or the page colour itself
                worst = min(cls._ratio(color, surface) for surface in cls.SURFACES)
                if worst < cls.FLOOR:
                    bad.append((color, round(worst, 2)))
        return bad

    def test_no_page_paints_text_below_the_floor(self):
        for page in self.PAGES:
            self.assertEqual(
                self._unreadable_text(_read(page)), [],
                f"{page} has text that fails the 4.5:1 contrast floor",
            )

    def test_the_check_catches_a_colour_that_fails(self):
        """A guard that cannot fail is decoration: feed it the two that did."""
        regressed = "<style>:root { --text-muted: #64748b; --accent: #5865F2; }" \
                    " p { color: var(--text-muted); } a { color: var(--accent); }</style>"
        found = dict((color, ratio) for color, ratio in self._unreadable_text(regressed))
        self.assertIn("#64748b", found)
        self.assertIn("#5865F2", found)

    def test_white_on_a_filled_button_is_measured_against_its_fill(self):
        filled = "<style>:root { --accent: #5865F2; }" \
                 " .btn { background: var(--accent); color: #fff; }</style>"
        self.assertEqual(self._unreadable_text(filled), [])


class TheAddressASignInIsCountedAgainst(unittest.TestCase):
    """A rate-limit bucket key must not be something the caller chose.

    Five wrong passkeys close the door for a quarter of an hour, which only
    means anything if the door is counted per person. Three sources are read, and
    the order is the claim: a *signed* stamp from the owner site's worker first,
    then the forwarded chain a trusted proxy appends to, then the peer address —
    the one nobody can write.
    """

    def _ip(self, headers: dict, *, secret: str = "shared-secret", trust: bool = True) -> str:
        with mock.patch.object(keep_alive, "OWNER_PROXY_SECRET", secret), \
             mock.patch.object(keep_alive, "_trust_proxy_headers", trust):
            with keep_alive.app.test_request_context("/owner/login", headers=headers):
                return keep_alive._client_ip()

    def test_a_signed_stamp_is_believed(self):
        self.assertEqual(
            self._ip({"X-Meow-Proxy": "shared-secret", "X-Meow-Client-IP": "203.0.113.9"}),
            "203.0.113.9",
        )

    def test_a_stamp_without_the_secret_is_ignored(self):
        """The header alone is a claim, and this is the whole reason it is signed."""
        self.assertNotEqual(self._ip({"X-Meow-Client-IP": "203.0.113.9"}), "203.0.113.9")

    def test_a_wrong_secret_is_ignored(self):
        self.assertNotEqual(
            self._ip({"X-Meow-Proxy": "guessed", "X-Meow-Client-IP": "203.0.113.9"}),
            "203.0.113.9",
        )

    def test_an_unconfigured_secret_trusts_nobody(self):
        """No secret in the environment is a deployment that has not opted in."""
        self.assertNotEqual(
            self._ip(
                {"X-Meow-Proxy": "shared-secret", "X-Meow-Client-IP": "203.0.113.9"},
                secret="",
            ),
            "203.0.113.9",
        )

    def test_a_stamp_that_is_not_an_address_is_ignored(self):
        for junk in ("", "not-an-ip", "203.0.113.9, 198.51.100.4", "203.0.113.9/24", "x" * 300):
            with self.subTest(stamp=junk):
                self.assertNotEqual(
                    self._ip({"X-Meow-Proxy": "shared-secret", "X-Meow-Client-IP": junk}),
                    junk,
                )

    def test_an_ipv6_stamp_is_kept_as_one_bucket_key(self):
        """Two spellings of one address would be two buckets."""
        self.assertEqual(
            self._ip({"X-Meow-Proxy": "shared-secret", "X-Meow-Client-IP": "2001:0db8::1"}),
            "2001:db8::1",
        )

    def test_the_forwarded_chain_is_still_the_fallback(self):
        self.assertEqual(self._ip({"X-Forwarded-For": "198.51.100.4"}), "198.51.100.4")

    def test_without_the_trust_flag_the_header_is_not_read_at_all(self):
        with keep_alive.app.test_request_context(
            "/owner/login", headers={"X-Forwarded-For": "198.51.100.4"}
        ):
            peer = keep_alive._client_ip()
        self.assertNotEqual(peer, "198.51.100.4")


class AFailedSendDoesNotReciteTheHost(unittest.TestCase):
    """What Discord said is for the log, not for the answer.

    An exception's text describes the inside of the process — a channel id, a
    status, a path — and the two endpoints that report a failed send answer over
    HTTP. The operator reads the log; the caller gets a sentence.
    """

    def test_the_exception_text_stays_out_of_the_answer(self):
        answer = keep_alive._send_error(
            RuntimeError("channel 1234: 403 Forbidden at /srv/app/bot.py")
        )
        for leaked in ("1234", "403", "/srv/app", "RuntimeError"):
            with self.subTest(leaked=leaked):
                self.assertNotIn(leaked, answer)
        self.assertIn("log", answer)

    def test_the_endpoints_use_it(self):
        """A helper nothing calls is not a fix."""
        source = _read("keep_alive.py")
        self.assertNotIn('f"Discord did not accept it: {e}"', source)


class TheForwardedResponseKeepsItsType(unittest.TestCase):
    """Every page the Space serves arrives with the type Flask gave it.

    The Space does not serve this app directly: ``app.py`` launches Gradio and
    mounts the Flask app on it as a catch-all, so the mount re-sends every page a
    browser asks for. It used to build its own response from the body and copy
    Flask's headers **minus ``content-type``** — which reads as de-duplication,
    because the response object it built sets its own type. It sets *none*: a
    starlette ``Response`` with no ``media_type`` carries no ``Content-Type`` at
    all. Chrome sniffs HTML it is not told the type of and renders the source, so
    the operator console's sign-in, the dashboard and both host pages were plain
    text in a browser while the identical bytes rendered correctly from Flask
    itself — the failure was in the *interpretation*, which is why nothing on the
    server side looked wrong. Measured before the fix: `text/html; charset=utf-8`
    from Flask on 127.0.0.1, and no ``Content-Type`` header at all from
    ``dacatowner.pages.dev/owner/login``.
    """

    NORMAL_HEADERS = [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", "16"),
        ("Set-Cookie", "meow_session=abc; Secure"),
    ]

    def test_html_is_still_html_after_the_mount(self):
        kwargs = keep_alive.forwarded_response(200, self.NORMAL_HEADERS, b"<!doctype html>")
        self.assertEqual(kwargs["media_type"], "text/html; charset=utf-8")
        self.assertEqual(kwargs["status_code"], 200)
        self.assertEqual(kwargs["content"], b"<!doctype html>")
        carried = [key.lower() for key, _ in keep_alive.carried_headers(self.NORMAL_HEADERS)]
        self.assertIn("set-cookie", carried, "the session cookie still travels")
        self.assertNotIn("content-type", carried, "declared once, on the response")
        self.assertNotIn("content-length", carried, "the length is the body's own")

    def test_repeated_headers_all_survive(self):
        """``Set-Cookie`` may appear more than once, and the response constructor
        takes a mapping — a second cookie handed to it as a dict disappears."""
        headers = [("Content-Type", "text/html"), ("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")]
        self.assertEqual(
            keep_alive.carried_headers(headers), [("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")]
        )

    def test_a_response_with_no_type_of_its_own_is_not_given_one(self):
        """A 204 or a bodiless redirect declares nothing, and must not be given a
        type nobody chose."""
        kwargs = keep_alive.forwarded_response(204, [("Content-Length", "0")], b"")
        self.assertNotIn("media_type", kwargs)

    def test_json_keeps_its_type_too(self):
        kwargs = keep_alive.forwarded_response(403, [("Content-Type", "application/json")], b"{}")
        self.assertEqual(kwargs["media_type"], "application/json")

    def test_the_response_the_mount_builds_carries_it_once(self):
        """The helper is only right if starlette accepts what it hands over.

        It once did not: the mount passed the carried headers straight to the
        response constructor, which takes a *mapping* and raises on a list, so the
        fix for the missing type would have turned every page of the console into
        a 500. The header is asserted through a real response object here, which
        is the only place the two halves meet — the local venv has no starlette,
        so it is skipped there.
        """
        try:
            from starlette.responses import Response
        except ImportError:  # pragma: no cover - the venv the suite runs in
            self.skipTest("starlette is only installed where the Space runs")
        body = b"<!doctype html>"
        response = Response(**keep_alive.forwarded_response(200, self.NORMAL_HEADERS, body))
        self.assertEqual(response.headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(response.headers["content-length"], str(len(body)))
        self.assertEqual(response.headers.getlist("content-type"), ["text/html; charset=utf-8"])
        cookies = [("Content-Type", "text/html"), ("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")]
        two = Response(**keep_alive.forwarded_response(200, cookies, body))
        for name, value in keep_alive.carried_headers(cookies):
            two.headers.append(name, value)
        self.assertEqual(two.headers.getlist("set-cookie"), ["a=1", "b=2"])

    def test_the_mount_actually_sends_it(self):
        """A helper nothing calls is not a fix — and this is the shape that lost
        the header, so it may not come back."""
        source = _read("app.py")
        self.assertIn("keep_alive.forwarded_response(", source)
        self.assertIn("keep_alive.carried_headers(", source)
        self.assertIn("response.headers.append(", source)
        self.assertNotIn('not in ("content-length", "content-type")', source)


if __name__ == "__main__":
    unittest.main()
