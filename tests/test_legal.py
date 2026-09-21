"""The Terms and the Privacy Policy must be *readable* in Discord.

`/tos` and `/privacy` used to slice the file at Discord's 4,096-character
description limit, so a longer policy lost its last sections silently — and both
documents in this repository are longer than that. These tests render the real
files through the real renderer and assert that every section arrives, that the
embed stays inside Discord's limits, and that the documents still cover the
things a member would look for: the market, the virtual currency and the
Supabase backup.
"""
import os
import unittest

import discord

from cogs import legal
from utils import features

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestLegalDocuments(unittest.TestCase):
    def setUp(self):
        self.terms = legal.read_document("TERMS.md")
        self.privacy = legal.read_document("PRIVACY.md")

    def test_both_documents_exist_and_are_not_empty(self):
        self.assertGreater(len(self.terms), 500, "TERMS.md looks empty")
        self.assertGreater(len(self.privacy), 500, "PRIVACY.md looks empty")

    def test_the_documents_are_longer_than_discords_description_limit(self):
        """The reason the section-by-section render exists — keep it honest."""
        self.assertGreater(max(len(self.terms), len(self.privacy)), legal.MAX_DESCRIPTION)

    def test_every_section_reaches_the_card_or_the_card_says_it_did_not(self):
        """Silence is the failure mode; a full card *or* an announced cut is not.

        The documents grew past Discord's 6,000-character ceiling when the Terms
        gained the anti-AI clause, the arbitration section and the indemnity — and
        the answer to that is not to shorten a legal document to fit a chat card.
        What must never happen is a section that simply is not there: either every
        heading arrives, or the card carries the field that says where the rest of
        it is, with the address of the published page.
        """
        for name, text in (("TERMS.md", self.terms), ("PRIVACY.md", self.privacy)):
            embed = legal.document_embed("Doc", text, name)
            rendered = "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)
            _, sections = legal.split_document(text)
            self.assertTrue(sections, f"{name} has no ## sections")
            complete = len(embed.fields) >= len(sections)
            if complete:
                for heading, _ in sections:
                    self.assertIn(heading, rendered, f"section {heading!r} was dropped")
            else:
                self.assertIn("The rest of it", rendered, f"{name} was cut with no notice")
                self.assertIn(legal.hub.marketing_url(), rendered)
                self.assertGreater(len(embed.fields), 3, f"{name} lost most of its body")

    def test_the_last_words_are_on_the_card_or_its_tail_is_announced(self):
        """The old renderer's failure mode: the tail of the file disappeared."""
        for name, text in (("TERMS.md", self.terms), ("PRIVACY.md", self.privacy)):
            embed = legal.document_embed("Doc", text, name)
            if len(embed.fields) < len(legal.split_document(text)[1]):
                self.assertIn("The rest of it", "\n".join(f.name for f in embed.fields))
                continue
            tail = text.strip().split("\n")[-1].strip()
            rendered = "\n".join(
                [embed.description or ""] + [f.value for f in embed.fields]
            )
            self.assertIn(tail, rendered, f"the end of {name} is missing")

    def test_the_embed_is_inside_every_discord_limit(self):
        for name, text in (("TERMS.md", self.terms), ("PRIVACY.md", self.privacy)):
            embed = legal.document_embed("Doc", text, name)
            self.assertLessEqual(len(embed.description or ""), legal.MAX_DESCRIPTION)
            self.assertLessEqual(len(embed.fields), legal.MAX_FIELDS)
            total = len(embed.description or "")
            for field in embed.fields:
                self.assertLessEqual(len(field.name), legal.MAX_FIELD_NAME)
                self.assertLessEqual(len(field.value), legal.MAX_FIELD_VALUE)
                total += len(field.value)
            self.assertLessEqual(total, legal.MAX_TOTAL_TEXT, f"{name} would be rejected")

    def test_an_oversized_document_points_at_the_rest_of_itself(self):
        """Nothing is dropped silently: a cut tail says where the rest is.

        Where it points is the half that was wrong. The old answer sent every
        reader to the repository, which is private — so the one line that exists
        to stop a document vanishing told a member to go and read something they
        cannot open. It points at the published page now, and the address is
        asserted rather than the wording.
        """
        huge = "# Doc\n\n## Section\n\n" + ("word " * 3000)
        embed = legal.document_embed("Doc", huge, "HUGE.md")
        rendered = "\n".join(f.value for f in embed.fields)
        self.assertIn(legal.hub.marketing_url(), rendered)
        self.assertNotIn("github.com", rendered)

    def test_a_document_that_fits_says_nothing_about_going_elsewhere(self):
        """The pointer is for a cut document, not for every document.

        A reader who is being shown the whole policy does not need a link to the
        whole policy; the field is noise, and it is the kind that makes a member
        wonder what was left out.
        """
        for name in ("PRIVACY.md", "TERMS.md"):
            text = legal.read_document(name)
            embed = legal.document_embed("Doc", text, name)
            _, sections = legal.split_document(text)
            if len(embed.fields) >= len(sections):
                rendered = "\n".join(f.name for f in embed.fields)
                self.assertNotIn("The rest of it", rendered, f"{name} is complete")

    def test_the_policies_cover_what_the_bot_actually_does(self):
        for needle in ("market", "virtual", "backup", "role"):
            self.assertIn(needle, self.terms.lower(), f"TERMS.md never mentions {needle}")
        for needle in ("market", "sqlite", "supabase", "backup"):
            self.assertIn(needle, self.privacy.lower(), f"PRIVACY.md never mentions {needle}")

    def test_the_third_parties_named_are_the_ones_the_code_uses(self):
        """A policy that lists the wrong companies is worse than none.

        This list is *who receives data*, and it changed with the repository's
        visibility in both directions at once. GitHub left it: the source is
        private, so the hosting it provides is to the project rather than to a
        visitor, and no member's data ever reaches it. Cloudflare joined it: the
        public site is served by Cloudflare Pages, so a visitor's IP reaches
        Cloudflare every time — which the policy did not say, because the line
        that should have said it was busy naming a company nothing talks to.
        """
        for host in ("binance", "tradingview", "cloudflare", "spotify", "tenor", "reddit"):
            self.assertIn(host, self.privacy.lower(), f"{host} is not disclosed")
        self.assertNotIn("github", self.privacy.lower(), "the source is not served to anyone")

    def test_the_commands_are_registered_and_still_gated_as_everyone(self):
        """One `/legal` command now shows both documents — they must stay open to
        every member, and both must still be reachable."""
        from utils import permissions

        self.assertEqual(permissions.COMMAND_ACCESS["legal privacy"], permissions.EVERYONE)
        self.assertEqual(permissions.COMMAND_ACCESS["legal tos"], permissions.EVERYONE)
        self.assertIn("cogs.legal", features.cog_paths())

        import discord

        legal_cog = legal.Legal(bot=None)
        self.assertEqual(
            sorted(c.name for c in legal_cog.legal_group.commands),
            ["privacy", "tos"],
        )
        self.assertIsInstance(legal_cog.legal_group, discord.app_commands.Group)

    def test_a_missing_file_says_so_rather_than_showing_nothing(self):
        self.assertEqual(legal.read_document("NOPE.md"), "")
        embed = legal.document_embed("Privacy Policy", "Document not found.")
        self.assertIn("not found", embed.description)


class TestThePoliciesMatchTheCode(unittest.TestCase):
    """A policy that describes a different bot is worse than no policy.

    Every claim below is checkable, and each one was wrong at least once: the
    retention numbers were hand-written prose, the dashboard's cookie and link
    lifetime were described from memory, and the policy named a font CDN that
    nothing loads any more while the chart library the market page *does* load
    went unnamed.
    """

    def setUp(self):
        # Flattened, because a claim is a claim wherever the markdown wraps it.
        def flat(name: str) -> str:
            return " ".join(legal.read_document(name).split()).lower()

        self.privacy = flat("PRIVACY.md")
        self.terms = flat("TERMS.md")

    def test_the_retention_windows_are_the_ones_the_code_enforces(self):
        from cogs import tracker
        from utils import scheduled

        for text, label in ((self.privacy, "PRIVACY.md"), (self.terms, "TERMS.md")):
            self.assertIn(
                f"{tracker.RETENTION_DAYS} days", text,
                f"{label} does not state the activity retention window",
            )
            self.assertIn(
                f"{scheduled.HISTORY_DAYS} days", text,
                f"{label} does not state the dashboard message-history window",
            )

    def test_the_backup_shape_is_the_one_the_code_keeps(self):
        from utils import cloud_sync

        self.assertIn("hourly snapshots", self.privacy)
        self.assertIn(str(cloud_sync.BACKUP_KEEP), self.privacy)
        # The bucket is private and the payload is raw, so the policy must not
        # promise encryption that does not exist.
        self.assertIn("private", self.privacy)

    def test_the_session_the_policy_describes_is_the_one_the_dashboard_sets(self):
        import keep_alive

        config = keep_alive.app.config
        self.assertIn(config["SESSION_COOKIE_NAME"].lower(), self.privacy)
        hours = int(config["PERMANENT_SESSION_LIFETIME"].total_seconds() // 3600)
        self.assertIn(f"{hours} hours", self.privacy)
        self.assertIn(f"{keep_alive.MAGIC_TTL_SECONDS // 60} minutes", self.privacy)

    def test_the_permission_the_policies_name_is_the_one_the_code_requires(self):
        """Both documents promised *Manage Server* while the code demanded Admin.

        The bar was raised to Administrator-or-owner deliberately — Manage Server
        is the bit Discord hands to a whole moderator role — but the sentence was
        left behind in three places, so a person granted exactly what the policy
        described was never listed. The bar is one bit in `utils.discord_auth`,
        so the text is checked against the code rather than against a sentence
        somebody remembers writing.
        """
        from utils import discord_auth

        self.assertEqual(
            discord_auth.ADMIN_BITS,
            discord_auth.ADMINISTRATOR,
            "the documented bar moved; PRIVACY.md, TERMS.md and templates/servers.html "
            "name it and must move in the same commit",
        )
        for text, label in ((self.privacy, "PRIVACY.md"), (self.terms, "TERMS.md")):
            self.assertIn("administrator", text, f"{label} never names the permission it needs")
            self.assertNotIn(
                "manage server",
                text,
                f"{label} promises Manage Server, which this code does not accept",
            )

        with open(os.path.join(ROOT, "templates", "servers.html"), encoding="utf-8") as handle:
            page = " ".join(handle.read().split()).lower()
        self.assertNotIn(
            "manage server", page, "/servers offers a permission the dashboard refuses"
        )
        self.assertIn("administrator", page)

        # And the sentence is *true*, driven rather than read: one guild, asked
        # for as a Manage-Server moderator and as an administrator.
        def listed(permissions: int, owner: bool = False) -> bool:
            guild = {
                "id": "1",
                "name": "Hmm",
                "permissions": str(permissions),
                "owner": owner,
            }
            return bool(discord_auth.servers_for([guild], "9", ["1"]))

        self.assertTrue(listed(discord_auth.ADMINISTRATOR), "an administrator is not listed")
        self.assertFalse(listed(discord_auth.MANAGE_GUILD), "Manage Server is still enough")
        self.assertTrue(listed(0, owner=True), "a server's owner is not listed")

    def test_the_chart_library_cdn_is_disclosed(self):
        """The one external asset a member's browser loads has to be named."""
        for host in ("unpkg.com", "cdn.jsdelivr.net"):
            self.assertIn(host, self.privacy, f"{host} is loaded by the chart page and unnamed")

    def test_a_bug_report_leaving_the_server_is_disclosed(self):
        """The cross-server transfer is the thing a member cannot see themselves."""
        for text, label in ((self.privacy, "PRIVACY.md"), (self.terms, "TERMS.md")):
            self.assertIn("bug report", text, f"{label} never mentions bug reports")
        self.assertIn("the bot's own server", self.privacy)

    def test_scheduled_messages_are_named_as_stored_content(self):
        for text, label in ((self.privacy, "PRIVACY.md"), (self.terms, "TERMS.md")):
            self.assertIn("scheduled", text, f"{label} does not mention scheduled messages")

    def test_the_two_documents_carry_the_same_effective_date(self):
        def date_of(name: str) -> str:
            for line in legal.read_document(name).splitlines():
                if line.startswith("**Effective Date:**"):
                    return line.split("·")[0].strip()
            return ""

        privacy, terms = date_of("PRIVACY.md"), date_of("TERMS.md")
        self.assertIn("Effective Date", privacy)
        self.assertEqual(privacy, terms, "the two policies disagree about when they took effect")


class TestDocumentedConfigurationIsReal(unittest.TestCase):
    """Every variable the template offers is one the code actually reads.

    Not tidiness. The template documented a backup encryption key and a Gist
    backup for a code path that had been deleted, which told an operator their
    database was encrypted at rest while it was a raw SQLite file in a private
    bucket — the kind of claim the Privacy Policy would then contradict. A
    variable nobody reads is a claim nobody should be making.
    """

    def test_every_variable_the_template_offers_is_read_by_the_code(self):
        import re
        from pathlib import Path

        root = Path(ROOT)
        names = re.findall(
            r"^([A-Z][A-Z0-9_]*)=", (root / ".env.example").read_text(), flags=re.MULTILINE
        )
        self.assertGreater(len(names), 5, "the template's assignments were not found")
        sources = [
            path.read_text()
            for pattern in ("*.py", "utils/*.py", "cogs/*.py")
            for path in root.glob(pattern)
        ]
        source = "\n".join(sources)
        unread = [name for name in names if name not in source]
        self.assertEqual(unread, [], f".env.example documents variables nothing reads: {unread}")


class TestTheAIDisclosure(unittest.TestCase):
    """The policies now say two things about AI, and both must stay true.

    The disclosure is worth having only because it is *specific*: AI assistants
    helped write the source, and **no AI runs in the Bot**. The second half is a
    factual claim about this tree, so it is checked against the tree — the day
    somebody adds a model call, the policies become false and this fails rather
    than leaving a document that reassures people about a flow that now exists.
    """

    @staticmethod
    def _read(name: str) -> str:
        with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
            return handle.read()

    def test_both_documents_disclose_the_assistance(self):
        for name in ("TERMS.md", "PRIVACY.md"):
            text = self._read(name)
            self.assertIn("AI coding assistants", text, f"{name} does not disclose it")
            self.assertIn("AI agents", text, f"{name} does not disclose AI agents")
            self.assertRegex(text, r"\*\*Version \d+\*\*", f"{name} was not re-versioned")

    def test_the_three_documents_agree_about_their_version(self):
        """One bundle, one version, one date — the digest the clickwrap records

        is computed over all three files, so a document left at an older version
        number is a document that was edited and not re-stated.
        """
        import re as _re

        versions = {}
        dates = {}
        for name in ("TERMS.md", "PRIVACY.md", "AI_POLICY.md"):
            text = self._read(name)
            version = _re.search(r"\*\*Version (\d+)\*\*", text)
            self.assertIsNotNone(version, f"{name} states no version")
            versions[name] = int(version.group(1))
            date = _re.search(r"\*\*Effective Date:\*\* ([^·\n]+)", text)
            self.assertIsNotNone(date, f"{name} states no effective date")
            dates[name] = date.group(1).strip()
        self.assertEqual(len(set(versions.values())), 1, f"the bundle disagrees: {versions}")
        self.assertGreaterEqual(max(versions.values()), 4, "the AI disclosure's version is gone")
        self.assertEqual(len(set(dates.values())), 1, f"the bundle disagrees on dates: {dates}")

    def test_the_anti_ai_claims_are_written_where_they_can_be_cited(self):
        """The framework's clause list, checked as text rather than remembered.

        Every one of these is a sentence the *law* side of this change depends on
        existing: an anti-AI clause, a clickwrap that records a revision, a
        liability cap, an indemnity, arbitration with a class-action waiver, and
        the routes a person can use to complain.
        """
        terms = " ".join(self._read("TERMS.md").split()).lower()
        privacy = " ".join(self._read("PRIVACY.md").split()).lower()
        ai = " ".join(self._read("AI_POLICY.md").split()).lower()

        for needle in (
            "automated collection is prohibited",
            "reserved against",
            "no licence",
            "arbitration",
            "class, collective or representative action",
            "indemnif",
            "twelve months",
            "grievance officer",
            "export-control",
            "warrant",
        ):
            self.assertIn(needle, terms, f"TERMS.md is missing {needle!r}")
        for needle in ("grievance officer", "data fiduciary", "30 days", "withdraw consent"):
            self.assertIn(needle, privacy, f"PRIVACY.md is missing {needle!r}")
        for needle in ("training", "robots.txt", "ai.txt", "180 days", "permission"):
            self.assertIn(needle, ai, f"AI_POLICY.md is missing {needle!r}")

    def test_the_clickwrap_revision_covers_the_anti_ai_policy(self):
        """A person has to have been shown the reservation, not just the Terms."""
        from utils import consent

        self.assertIn(("AI_POLICY.md", "AI &amp; automated use"), consent.DOCUMENTS)
        self.assertIn("AI_POLICY.md", consent.BUNDLE_EXTRAS + tuple(
            name for name, _ in consent.DOCUMENTS
        ))
        self.assertRegex(consent.version(), r"^[0-9a-f]{12}$")

    def test_the_claim_that_no_ai_runs_is_still_true(self):
        # Grepped rather than asserted from memory: a provider named in an import,
        # a URL, or a client constructor is the way this claim would actually stop
        # being true. The patterns are about *calling* one, not about naming one:
        # `utils/ai_guard.py` names OpenAI, Anthropic, ByteDance and the rest
        # because that is how a refusal recognises a crawler, and a policy that
        # could not name the crawler it refuses would be a policy that cannot
        # enforce anything. A bare mention is therefore not a match; an import, an
        # API host or a key is.
        import re
        from pathlib import Path

        root = Path(ROOT)
        patterns = ("*.py", "utils/*.py", "cogs/*.py", "scripts/*.py")
        called = re.compile(
            r"(^|\s)(import|from)\s+(openai|anthropic|cohere|mistralai|google\.generativeai)\b"
            r"|api\.openai\.com|api\.anthropic\.com|generativelanguage\.googleapis\.com"
            r"|huggingface\.co/|api-inference\.huggingface"
            r"|[A-Z]*(OPENAI|ANTHROPIC|COHERE|MISTRAL|HUGGINGFACE)[A-Z_]*_?(API_)?KEY",
            flags=re.IGNORECASE | re.MULTILINE,
        )
        found = []
        for pattern in patterns:
            for path in root.glob(pattern):
                if path.name.startswith("test_"):
                    continue
                body = path.read_text(encoding="utf-8", errors="ignore")
                for match in called.finditer(body):
                    found.append(f"{path.name}: {match.group(0).strip()}")
        self.assertEqual(
            found, [],
            "PRIVACY.md states that no AI runs in the Bot, and this says otherwise: "
            f"{found}. Update the disclosure in the same commit.",
        )

    def test_the_directory_listing_copy_matches_the_registry(self):
        """The listing is derived from the registry, so it cannot go stale alone.

        Both numbers in `docs/discord-directory.md` were written by hand and one
        of them was wrong within the hour it was written. This is the guard: the
        copy is part of the repository now, so a feature added without touching it
        fails here rather than in a reviewer's reading of a stale listing.
        """
        with open(os.path.join(ROOT, "docs", "discord-directory.md"), encoding="utf-8") as handle:
            doc = handle.read()

        # Unwrapped first. The document is prose, so a sentence is allowed to
        # cross a line — and it does: the copy is quoted at 80 columns, which is
        # why the first version of this test failed on text that was present and
        # merely folded. What must not happen is a *different* number.
        def unwrapped(text: str) -> str:
            lines = [line.lstrip("> ").rstrip() for line in text.splitlines()]
            return " ".join(part for part in lines if part)

        flat = unwrapped(doc)
        self.assertIn(
            f"{features.system_count()} systems and {features.command_count()} slash commands",
            flat,
        )
        short = (
            "A community bot for moderation, tickets with transcripts, leveling, an economy "
            "with a shop and a live market, a starboard, a 365-day activity tracker, and a "
            "web dashboard with a message composer."
        )
        self.assertIn(short, flat)
        self.assertIn(f"({len(short)} characters", flat)

    def test_the_invite_links_the_document_names_are_the_ones_that_exist(self):
        # The document told a submitter to look in `cogs/help.py`, where the links
        # have never been. Pinned so the next reader is sent to the right file.
        from utils import discord_auth

        with open(os.path.join(ROOT, "docs", "discord-directory.md"), encoding="utf-8") as handle:
            doc = handle.read()

        self.assertIn("utils/discord_auth.py", doc)
        self.assertIn("INVITE_PERMISSIONS", doc)
        self.assertIn("MINIMAL_INVITE_PERMISSIONS", doc)
        self.assertNotEqual(
            discord_auth.INVITE_PERMISSIONS,
            discord_auth.MINIMAL_INVITE_PERMISSIONS,
            "the two invite links are no longer two different scopes",
        )
        self.assertNotIn("cogs/help.py", doc)


if __name__ == "__main__":
    unittest.main()
