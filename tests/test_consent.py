"""The clickwrap: which text was agreed to, and whether saying no works.

Browsewrap is what this project had — a link in a footer and a sentence about
continuing to use the Bot — and the framework behind this change is blunt about
what that is worth against a collector: a bot cannot assent to a hidden link, so
a prohibition it never saw was never a term. What replaces it is an affirmative
act at the door, recorded against a *revision of the text* rather than against a
date somebody typed.

Four claims, and each is a class below:

* **The revision is the document.** Not a hand-kept number: the digest of the
  files, so a sentence added in September cannot be covered by an agreement made
  in June. The test drives that by moving the bytes and watching the revision move.
* **The door asks, and nothing else does.** A sign-in lands on the agreement, and
  a visitor reading the policies never sees it — a policy behind an acceptance
  wall is worse than no policy.
* **Two answers, one size.** Declining signs the session out, says so plainly,
  records nothing, and is reachable by the same button markup as accepting.
* **A record that cannot be written says so.** The person still gets in, the page
  says the ledger is missing an entry, and `/health` agrees — the failure mode
  worth testing is the one where a clickwrap silently stops recording and keeps
  looking like it works.
"""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import unittest
from unittest import mock

import keep_alive
from tests.support import TempDatabaseMixin, agree_to_the_policies, agreed_next_url
from utils import consent

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TheRevision(unittest.TestCase):
    def test_it_is_the_content_and_not_a_number_somebody_keeps(self):
        with tempfile.TemporaryDirectory() as folder:
            first = os.path.join(folder, "A.md")
            second = os.path.join(folder, "B.md")
            with open(first, "w", encoding="utf-8") as handle:
                handle.write("# Terms\n\n## 1. A clause\n")
            with open(second, "w", encoding="utf-8") as handle:
                handle.write("# Privacy\n")
            with mock.patch.object(consent, "_document_paths", return_value=[first, second]):
                consent._version_cache = None
                before = consent.version()
                self.assertTrue(before)
                # A single word added to one of the documents is a new revision,
                # which is the property a hand-kept `Version 4` cannot have.
                with open(second, "a", encoding="utf-8") as handle:
                    handle.write("\n## 2. One more sentence.\n")
                os.utime(second, (os.path.getmtime(second) + 5, os.path.getmtime(second) + 5))
                after = consent.version()
            consent._version_cache = None
            self.assertNotEqual(before, after)

    def test_it_is_cached_against_the_files_rather_than_rehashed_per_request(self):
        consent._version_cache = None
        first = consent.version()
        self.assertEqual(consent.version(), first)
        self.assertIsNotNone(consent._version_cache)

    def test_a_missing_document_refuses_to_invent_one(self):
        with mock.patch.object(consent, "_document_paths", return_value=["/nonexistent/x.md"]):
            consent._version_cache = None
            self.assertEqual(consent.version(), "")
        consent._version_cache = None

    def test_the_documents_it_covers_are_the_documents_it_shows(self):
        """A document added to the bundle without a page is a link to nowhere."""
        for filename, _title in consent.DOCUMENTS:
            self.assertIn(filename, keep_alive.AGREE_HREFS, f"{filename} has no page")
            self.assertTrue(
                os.path.isfile(os.path.join(ROOT, filename)),
                f"{filename} is agreed to and does not exist",
            )
        for filename in keep_alive.AGREE_HREFS:
            self.assertIn(filename, dict(consent.DOCUMENTS))

    def test_the_label_reads_the_documents_own_version_line(self):
        self.assertIn("version", consent.document_label("TERMS.md").lower())


class NeedsAgreement(TempDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.use_temp_database("consent.db")
        self.client = keep_alive.app.test_client()

    def tearDown(self):
        self.release_temp_database()
        consent._summary = None
        consent._write_failed = False

    def _sign_in(self):
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["auth_scope"] = "global"
            session["csrf_token"] = "csrf-token"

    def test_a_visitor_is_never_asked(self):
        self.assertEqual(self.client.get("/privacy").status_code, 200)
        self.assertEqual(self.client.get("/terms").status_code, 200)
        self.assertEqual(self.client.get("/ai").status_code, 200)
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_the_door_wraps_the_destination(self):
        with self.client.session_transaction() as session:
            session["csrf_token"] = "csrf-token"
        with mock.patch.object(keep_alive, "ADMIN_PASSKEY", "secret-passkey"):
            response = self.client.post(
                "/login",
                data={"passkey": "secret-passkey", "csrf_token": "csrf-token"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].startswith("/agree"))
        self.assertEqual(agreed_next_url(response.headers["Location"]), "/dashboard")

    def test_nothing_is_recorded_until_one_of_the_answers_is_pressed(self):
        self._sign_in()
        before = consent.reading()
        self.assertEqual(self.client.get("/agree").status_code, 200)
        self.assertEqual(consent.reading(), before, "reading the page recorded something")

    def test_the_page_carries_both_answers_at_the_same_weight(self):
        self._sign_in()
        body = self.client.get("/agree").get_data(as_text=True)
        # The two answers, and not the page frame's own buttons: the shared
        # header carries a menu toggle and the theme popover, so counting every
        # <button> on the page counts the site rather than the decision. What
        # this test is about is the pair inside the decision block.
        decision = re.search(r'<div class="decision">(.*?)</div>', body, re.S)
        self.assertIsNotNone(decision, "the decision block is missing")
        buttons = re.findall(r"<button[^>]*>", decision.group(1))
        self.assertEqual(len(buttons), 2, "one of the two answers is missing")
        classes = {re.search(r'class="([^"]+)"', button).group(1) for button in buttons}
        self.assertEqual(len(classes), 1, f"the two answers are styled differently: {classes}")
        self.assertEqual(len(re.findall(r"<h1", body)), 1)
        self.assertIn('lang="en"', body)
        self.assertIn("skip-link", body, "the page has no skip link")

    def test_the_revision_is_shown_so_it_can_be_compared_later(self):
        self._sign_in()
        body = self.client.get("/agree").get_data(as_text=True)
        self.assertIn(consent.version(), body)
        for filename, _title in consent.DOCUMENTS:
            self.assertIn(keep_alive.AGREE_HREFS[filename], body)

    def test_a_visitor_who_is_not_signed_in_is_sent_to_sign_in(self):
        response = self.client.get("/agree")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_accepting_records_the_revision_the_scope_and_the_browser(self):
        self._sign_in()
        self.assertEqual(
            agree_to_the_policies(self.client, next_url="/dashboard").headers["Location"],
            "/dashboard",
        )
        rows = consent.reading()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["version"], consent.version())
        self.assertEqual(rows[0]["scope"], "global")
        self.assertNotIn("203.0", rows[0].get("ip") or "", "the address is not masked")

    def test_the_recorded_address_is_masked_in_the_reading(self):
        consent.accept(scope="global", ip="203.0.113.9", agent="Mozilla/5.0 (X11)")
        rows = consent.reading()
        self.assertTrue(rows)
        self.assertIn("203.0.x.x", rows[0]["ip"])

    def test_declining_signs_out_and_records_nothing(self):
        self._sign_in()
        before = len(consent.reading())
        response = agree_to_the_policies(self.client, decision="decline")
        body = response.get_data(as_text=True)
        self.assertIn("signed out", body.lower())
        self.assertEqual(len(consent.reading()), before, "a refusal was recorded as an agreement")
        with self.client.session_transaction() as session:
            self.assertIsNone(session.get("authenticated"))
            self.assertIsNone(session.get(consent.SESSION_KEY))

    def test_a_form_without_the_token_is_refused_and_records_nothing(self):
        self._sign_in()
        before = len(consent.reading())
        response = self.client.post("/agree", data={"decision": "accept"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(consent.reading()), before)

    def test_an_unwritable_ledger_does_not_lock_anybody_out(self):
        """The person agreed. A host that cannot write the record is a fact about
        the host, and refusing entry over it punishes the wrong party."""
        self._sign_in()
        with mock.patch.object(consent, "_connect", side_effect=sqlite3.Error("read only")):
            response = agree_to_the_policies(self.client, next_url="/dashboard")
        body = response.get_data(as_text=True)
        self.assertIn("could not be written", body)
        with self.client.session_transaction() as session:
            self.assertEqual(session.get(consent.SESSION_KEY), consent.version())
        self.assertFalse(consent.writable())

    def test_health_reports_the_revision_and_no_addresses(self):
        self._sign_in()
        agree_to_the_policies(self.client)
        consent._summary = None
        block = self.client.get("/health").get_json()["consent"]
        self.assertEqual(block["revision"], consent.version())
        self.assertIn("accepted", block)
        for key in ("ip", "agent", "label", "user_id"):
            self.assertNotIn(key, block)

    def test_an_unknown_revision_asks_again(self):
        """The September clause is not covered by the June agreement."""
        self._sign_in()
        agree_to_the_policies(self.client)
        with self.client.session_transaction() as session:
            session[consent.SESSION_KEY] = "000000000000"
        body = self.client.get("/agree").get_data(as_text=True)
        self.assertIn("Before you carry on", body)


class WhatThePageNeverDoes(unittest.TestCase):
    """The dark patterns the framework names, checked as absences.

    Symmetry is the property a regulator actually looks for, and it is easy to
    lose by accident — a checkbox added later, a "no thanks" that shames the
    reader, a countdown somebody thinks is friendly. Absences are the right shape
    for this: the page is allowed to grow, as long as it does not grow one of
    these.
    """

    def setUp(self):
        raw = open(os.path.join(ROOT, "templates", "agree.html"), encoding="utf-8").read()
        # Comments are stripped before the check, and that is deliberate: this
        # file's own header explains that there is *no* countdown or
        # confirmshaming on the page, and a test that greps the whole file would
        # fail on the paragraph saying so. The repository has made this mistake
        # before — a check that forbade the string `index.html` broke on the
        # comment explaining why it was gone.
        self.body = re.sub(r"\{#.*?#\}", "", raw, flags=re.DOTALL)

    def test_nothing_is_pre_selected(self):
        self.assertNotIn("checked", self.body)
        self.assertNotIn("selected", self.body)

    def test_no_confirmshaming_and_no_false_urgency(self):
        lowered = self.body.lower()
        for phrase in (
            "are you sure",
            "no thanks",
            "no, i don't want",
            "miss out",
            "special offer",
            "expires in",
            "hurry",
        ):
            self.assertNotIn(phrase, lowered, f"the page shames or rushes the reader: {phrase!r}")
        self.assertNotIn("countdown", lowered)
        self.assertNotIn("timer", lowered)

    def test_neither_answer_is_the_default_one(self):
        self.assertNotIn("autofocus", self.body)
        self.assertNotIn("btn-primary", self.body, "one answer is drawn as the accent action")

    def test_the_notice_is_announced_rather_than_silent(self):
        """A state change a screen reader is not told about is a state change the
        reader did not experience."""
        self.assertIn('role="{% if notice_bad %}alert{% else %}status{% endif %}"', self.body)


if __name__ == "__main__":
    unittest.main()
