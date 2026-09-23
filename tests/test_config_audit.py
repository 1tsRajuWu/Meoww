"""The configuration surface must be knowable, and the report must be truthful.

Everything here exists because of one shape of failure, which this repository has
now hit three times: **a setting that is configured-and-off looks exactly like a
feature that is broken.**

* ``DISCORD_CLIENT_ID`` was declared in ``render.yaml`` and absent from the
  running service, so the live site silently rendered one button fewer;
* the site's deploy skipped publishing for four commits while reporting success;
* and ``load_dotenv()`` was called *after* ``keep_alive`` and ``utils`` were
  imported, so a ``.env`` — the documented way to configure this locally — reached
  ``DISCORD_TOKEN`` and nothing else, leaving the dashboard on a random session
  key with passkey sign-in switched off and backups disabled on a machine where
  all three were filled in.

So the tests are about the three ways ``utils.config_audit`` could stop being
worth reading:

1. **drift** — a setting the code reads that the registry does not know, or a
   registry entry nothing reads. Either one makes the report a partial answer
   presented as a complete one, which is worse than no report;
2. **crying wolf** — a finding on a deployment where the absence costs nothing.
   A report that shouts every boot is a report nobody reads by the third deploy,
   which is the failure it was written to prevent. ``implied_by`` is the escape
   hatch and it is tested in both directions;
3. **leaking** — a value, anywhere. The endpoint can only be served to an
   administrator, but half of these settings are credentials and the property
   should not depend on the route being guarded correctly.
"""
import json
import os
import re
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import keep_alive
from utils import config_audit

ROOT = Path(__file__).resolve().parent.parent

#: The part of the tree that runs the deployment, as opposed to the tooling that
#: builds the published sites. These are the directories the "one reader" rule
#: below covers; ``scripts/`` is exempt because it runs on a different machine,
#: which is also why the reports keep a ``scope`` per setting.
RUNTIME_DIRS = ("cogs", "utils")
RUNTIME_FILES = ("bot.py", "keep_alive.py")

#: The one module allowed to read the environment for the runtime. Everything
#: else asks it, which is what makes the registry the whole answer to "what can
#: this deployment be configured with" instead of a second list beside the code.
SETTINGS_READER = "utils/config_audit.py"

#: Directories whose Python is not this application. ``deleted_features`` is
#: exactly what it says: retired code kept for reference, with its own tests, and
#: none of it is loaded, imported or deployed. Its environment reads are not this
#: deployment's surface.
SKIP_DIRS = {"deleted_features", "tests", ".venv", "venv", "_site", "__pycache__", ".git"}

#: Every way this tree reaches for the environment, including the two indirections
#: that a plain ``os.getenv`` search misses: a name handed to ``_env_flag``, and a
#: module-level constant that holds the *name* rather than the value —
#: ``ENV = "RENDER_DEPLOY_HOOK_URL"``, ``SELLING_ENV = "PLAN_CHECKOUT"``.
READ_PATTERNS = (
    r"""config_audit\.(?:get|flag|is_set)\(\s*["']([A-Z][A-Z0-9_]*)["']""",
    r"""os\.getenv\(\s*["']([A-Z][A-Z0-9_]*)["']""",
    r"""os\.environ\.get\(\s*["']([A-Z][A-Z0-9_]*)["']""",
    r"""os\.environ(?:\.setdefault|\.pop)?\(\s*["']([A-Z][A-Z0-9_]*)["']""",
    r"""os\.environ\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]""",
    r"""_env_flag\(\s*["']([A-Z][A-Z0-9_]*)["']""",
    r"""^[A-Z_]*ENV\b\s*[:=][^=\n]*["']([A-Z][A-Z0-9_]+)["']""",
)

#: A mapping whose *name* ends in ENV holds setting names as its values, one per
#: line — ``CHECKOUT_ENV = {"pro": "LEMONSQUEEZY_CHECKOUT_PRO"}``. Those values are
#: read indirectly, through the mapping, so no literal reaches ``os.getenv`` and a
#: plain search cannot see them.
#:
#: Scanned as a block rather than matched line-by-line, because the general form of
#: that pattern caught ``{"label": "RAJ"}`` in the dashboard preview harness —
#: ordinary data, all-caps, nothing to do with the environment. A guard that fails
#: for a fixture's display name is a guard somebody deletes.
_ENV_MAPPING_OPENS = re.compile(r'''^[A-Z_]*ENV\b[^=\n]*=\s*\{''')
_ENV_MAPPING_ENTRY = re.compile(
    r'''^\s*["'][a-z_]+["']\s*:\s*["']([A-Z][A-Z0-9_]+)["']'''
)


def application_sources() -> list[Path]:
    """Every Python file that ships as this application."""
    found: list[Path] = []
    for path in ROOT.rglob("*.py"):
        parts = set(path.relative_to(ROOT).parts[:-1])
        if parts & SKIP_DIRS:
            continue
        found.append(path)
    return sorted(found)


def mapping_entries(text: str) -> list[str]:
    """The setting names held as the *values* of each ``*_ENV`` mapping."""
    names: list[str] = []
    inside = False
    for line in text.splitlines():
        if not inside and _ENV_MAPPING_OPENS.match(line):
            inside = True
            continue
        if inside:
            if line.strip().startswith("}"):
                inside = False
                continue
            entry = _ENV_MAPPING_ENTRY.match(line)
            if entry:
                names.append(entry.group(1))
    return names


def names_the_code_reads() -> dict[str, set[str]]:
    """Every environment name the application reads, and where."""
    found: dict[str, set[str]] = {}
    for path in application_sources():
        text = path.read_text(encoding="utf-8")
        for pattern in READ_PATTERNS:
            for name in re.findall(pattern, text, flags=re.MULTILINE):
                found.setdefault(name, set()).add(str(path.relative_to(ROOT)))
        for name in mapping_entries(text):
            found.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    return found


class TheRegistryIsTheWholeTruth(unittest.TestCase):
    """A report is only worth reading if it is complete, in both directions."""

    def test_every_setting_the_code_reads_is_declared(self):
        """The direction that matters most: an undeclared setting is invisible.

        This is the test that would have caught all three failures named in the
        module docstring, because each of them was a name nobody had written down.
        It caught one in the first draft of this registry, too — the first version
        omitted ``DISCORD_CLIENT_ID``, the variable the whole incident turned on.
        """
        declared = set(config_audit.BY_NAME)
        read = names_the_code_reads()
        undeclared = sorted(name for name in read if name not in declared)
        self.assertEqual(
            undeclared,
            [],
            "the code reads settings the audit does not know about, so its report "
            f"is a partial answer presented as a whole one: {undeclared} "
            f"(read by {[sorted(read[n]) for n in undeclared]})",
        )

    def test_every_setting_the_registry_declares_is_a_setting_the_code_reads(self):
        """The other direction: an entry nothing reads is a claim about nothing.

        The reverse of the same defect, and the reason ``.env.example`` has its own
        guard: a template once documented a backup key for a code path that had
        been deleted, telling an operator their database was encrypted when it was
        a raw SQLite file.
        """
        read = set(names_the_code_reads())
        declared = {setting.name for setting in config_audit.SETTINGS}
        unread = sorted(declared - read)
        self.assertEqual(
            unread,
            [],
            f"the audit describes settings nothing in this tree reads: {unread}",
        )

    def test_no_setting_is_declared_twice(self):
        names = [setting.name for setting in config_audit.SETTINGS]
        self.assertEqual(len(names), len(set(names)), "a name appears twice")

    def test_every_entry_is_classified_and_says_something(self):
        """No blank prose, and no kind the renderers cannot order.

        ``SEVERITY`` is indexed into, so an unlisted kind would raise at boot
        rather than here — and boot is the surface this module exists to fill.
        """
        for setting in config_audit.SETTINGS:
            with self.subTest(setting=setting.name):
                self.assertIn(setting.kind, config_audit.SEVERITY)
                self.assertIn(setting.scope, (config_audit.RUNTIME, config_audit.BUILD))
                self.assertTrue(setting.what, "no description of what it turns on")
                self.assertGreater(
                    len(setting.cost), 20,
                    "the cost of the absence is what a reader came for",
                )
                for implied in setting.implied_by:
                    self.assertIn(implied, config_audit.BY_NAME)


class TheRuntimeReachesTheEnvironmentThroughOneModule(unittest.TestCase):
    """A registry that describes reads it does not perform can drift from them.

    ``config_audit`` began as a *description* of ~50 ``os.getenv`` calls spread
    across the tree, held to them only by the parity test above. That test proves
    the two lists agree about **names**; it cannot see a default, a stripping
    rule or a truthy-set that differs from what the audit's prose promises. The
    reader is what closes that gap, and this guard is what keeps the reader the
    only one — a new ``os.environ`` lookup in the runtime is a new place where a
    setting can be spelled differently from how the registry describes it.

    It is a source walk rather than an import-time check because the failure it
    has to catch is an *added* read, which no runtime assertion can see.
    """

    def runtime_sources(self) -> list[Path]:
        found = [ROOT / name for name in RUNTIME_FILES]
        for directory in RUNTIME_DIRS:
            found.extend(sorted((ROOT / directory).glob("*.py")))
        return [path for path in found if path.exists()]

    def test_only_the_settings_module_reads_the_environment(self):
        offenders: list[str] = []
        for path in self.runtime_sources():
            relative = str(path.relative_to(ROOT))
            if relative == SETTINGS_READER:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if re.search(r"os\.(?:getenv|environ)", line):
                    offenders.append(f"{relative}:{number}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "these read the environment directly instead of through "
            f"config_audit.get/flag, so the registry is no longer the whole "
            "surface: " + "; ".join(offenders),
        )

    def test_the_walk_actually_covers_the_application(self):
        """A guard that found no files would pass the test above forever."""
        sources = {str(path.relative_to(ROOT)) for path in self.runtime_sources()}
        self.assertIn("bot.py", sources)
        self.assertIn("keep_alive.py", sources)
        self.assertIn("utils/db.py", sources)
        self.assertIn("cogs/economy.py", sources)
        self.assertGreater(len(sources), 20)


class NoPublicPageNamesASetting(unittest.TestCase):
    """The rule the whole audit follows, applied to what a stranger can read.

    ``/health`` was rewritten for it — counts and never names, because "this
    deployment has no sign-in secret" is a reconnaissance hint assembled for
    free — and the hub was built under it. Nothing checked the *other* pages, and
    `/login` was naming `DISCORD_CLIENT_SECRET` in the sentence it shows when
    sign-in is switched off: the one page a visitor reaches by trying to sign in,
    telling them which credential is missing.

    Driven over the real app rather than over the templates, because a name can
    reach a page from a Python string as easily as from markup — which is
    exactly how this one reached it.
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("DISCORD_CLIENT_ID", "1548380269776605324")
        cls.client = keep_alive.app.test_client()

    def _public_pages(self):
        """Every GET route that answers a signed-out visitor, rendered.

        Parameterised routes are skipped: what they answer depends on the
        argument, and the pages that carry a deployment's own state are behind
        the sign-in anyway. That is a boundary, not a hole — a route added here
        is a route that now has to keep this rule.
        """
        for rule in sorted(keep_alive.app.url_map.iter_rules(), key=lambda r: r.rule):
            if rule.rule.startswith(("/static", "/hub/static")) or "<" in rule.rule:
                continue
            if "GET" not in (rule.methods or ()):
                continue
            response = self.client.get(rule.rule, follow_redirects=False)
            if response.status_code == 200:
                yield rule.rule, response.get_data(as_text=True)

    def test_no_signed_out_page_names_a_setting(self):
        pages = list(self._public_pages())
        self.assertGreater(len(pages), 6, "the public surface is not being walked")
        for path, body in pages:
            for setting in config_audit.SETTINGS:
                self.assertNotIn(
                    setting.name, body,
                    f"{path} names {setting.name} to a signed-out visitor",
                )

    def test_the_audit_itself_still_names_every_one_of_them(self):
        """The rule is about the *public* surface, not about the code.

        An operator reading the signed-in audit is told exactly which setting is
        missing — that is the feature. If this test ever starts failing because
        the list shrank, the reporting has been silenced rather than the leak
        closed.
        """
        self.assertGreater(len(config_audit.SETTINGS), 40)
        self.assertTrue(any(s.secret for s in config_audit.SETTINGS))


class DocumentationAgreesWithTheRegistry(unittest.TestCase):
    """The template and the host manifest are the human half of this surface."""

    def setUp(self):
        self.template = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")
        self.deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

    def test_the_template_offers_every_setting_whose_absence_costs_something(self):
        """``.env.example`` is the documented way to configure this locally.

        If a setting whose absence switches off a feature is not in it, then the
        documented path ships a deployment with that feature off — which is the
        ``.env`` half of the defect this pass fixed, arriving through the
        documentation instead of the import order.

        It found five when it was written: the update channel and role, the deploy
        hook, the owner id, and ``DASHBOARD_HTTPS``.
        """
        documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", self.template, flags=re.MULTILINE))
        missing = sorted(
            setting.name
            for setting in config_audit.SETTINGS
            if setting.notable and setting.name not in documented
        )
        self.assertEqual(
            missing,
            [],
            f"a setting whose absence turns something off is undocumented: {missing}",
        )

    def test_the_host_manifest_declares_everything_needed_to_be_safe(self):
        """An essential or security setting must be offered by the deploy.

        Narrow on purpose. ``render.yaml`` is a Render manifest, so settings a
        Render host already supplies — the ``implied_by`` ones — are correctly
        absent from it, and demanding a prompt for every optional capability would
        turn the blueprint into a wall nobody finishes filling in. What must not
        happen again is the specific omission recorded in its own comments: the
        secret key and the passkey were documented in the README, present in
        ``.env.example``, and undeclared here, so production ran without them.

        The match is a declaration line, not the name appearing anywhere in the
        file — and that distinction was found by mutating this test rather than by
        reading it. Deleting ``- key: FLASK_SECRET_KEY`` did *not* fail the first
        version, because the paragraph above this entry explains the omission and
        names the variable in prose. A check satisfied by a comment about the
        setting is a check that passes on exactly the edit it exists to catch.
        """
        declared = set(
            re.findall(r"^\s*- key:\s*([A-Z][A-Z0-9_]*)", self.blueprint, flags=re.MULTILINE)
        )
        self.assertGreater(len(declared), 3, "the blueprint's declarations were not found")
        missing = sorted(
            setting.name
            for setting in config_audit.SETTINGS
            if setting.scope == config_audit.RUNTIME
            and setting.kind in (config_audit.ESSENTIAL, config_audit.SECURITY)
            and not setting.implied_by
            and setting.name not in declared
        )
        self.assertEqual(
            missing,
            [],
            f"render.yaml does not offer a setting the deployment needs: {missing}",
        )


    def the_address_the_code_falls_back_to(self) -> str:
        """The Render hostname this deployment is reached at.

        Asked of the function that decides it, under the environment of a Render
        host, rather than read from a copy of the string: the fact under test is
        that a *name* and an *address* are different things, and a second literal
        here would be one more copy of the address to drift.
        """
        with mock.patch.dict(os.environ, {"RENDER": "true"}, clear=True):
            return urllib.parse.urlsplit(keep_alive.dashboard_base_url()).hostname or ""

    def test_the_manifest_names_the_service_the_account_has(self):
        """A sync matches on **name**, so a name the account does not have creates a bot.

        ``render.yaml`` declared ``meow-discord-bot`` — which is the service's
        *address* (``meow-discord-bot.onrender.com``, the subdomain it was created
        with) — while the service that runs this bot is named ``Meoww``. Render
        updates the service whose name the Blueprint declares and creates one when
        there is no match, so that mismatch minted ``meow-discord-bot-fmhp``
        alongside the real service on 2026-09-19. The duplicate had no environment
        variables (the prompts are answered per service and nobody answered them
        for it), so every one of its deploys died at boot in
        ``RuntimeError: DISCORD_TOKEN not found`` and Render never ran an instance
        on it — a red deploy on every push, next to a service doing its job.

        A file cannot ask Render what a service is called, so the account drifting
        away from the repository is a dashboard fact this test cannot see. What it
        can see is the shape the drift took, and both halves of it: the name is
        written down twice here (the manifest's declaration and the deployment
        guide's record of it) and those two must agree; the address is likewise
        written twice (that same line and the fallback the code reaches a Render
        host at) and those must agree; and the one string may never be used as the
        other, which is what the edit that caused this looks like.
        """
        declared = re.findall(r"^\s{4}name:\s*(\S+)\s*$", self.blueprint, flags=re.MULTILINE)
        self.assertEqual(len(declared), 1, "there should be exactly one service in the manifest")
        recorded = re.findall(r"^\s*- Service:\s*(\S+)", self.deployment, flags=re.MULTILINE)
        self.assertEqual(len(recorded), 1, "docs/deployment.md should record the service once")
        self.assertEqual(
            declared[0],
            recorded[0],
            "render.yaml and docs/deployment.md disagree about the service's name",
        )

        address = self.the_address_the_code_falls_back_to()
        self.assertTrue(address, "the deployment has no address to compare a name against")
        stated = re.findall(r"^\s*- Address:\s*(\S+)", self.deployment, flags=re.MULTILINE)
        self.assertEqual(len(stated), 1, "docs/deployment.md should record the address once")
        self.assertEqual(
            urllib.parse.urlsplit(stated[0]).hostname,
            address,
            "the address the guide records is not the one the code reaches this host at",
        )

        name = declared[0]
        self.assertNotIn(
            ".",
            name,
            f"{name!r} reads as a hostname; the manifest's name is the dashboard name",
        )
        self.assertNotEqual(
            name.lower(),
            address.split(".")[0].lower(),
            f"{name!r} is this service's address, not its name: a Blueprint sync would "
            "create a second service instead of updating the one that runs the bot",
        )


class AbsenceIsReportedWhereSomebodyIsLooking(unittest.TestCase):
    """The renderers, driven with environments that are not this one."""

    def test_the_boot_log_names_the_essential_when_it_is_missing(self):
        lines = config_audit.boot_lines({})
        blob = "\n".join(lines)
        self.assertIn("DISCORD_TOKEN", blob)
        self.assertIn("ESSENTIAL", blob)

    def test_the_boot_log_is_not_alarming_when_everything_is_set(self):
        """The one state that must stay quiet, or the report is wallpaper."""
        complete = {
            setting.name: "x"
            for setting in config_audit.SETTINGS
            if setting.scope == config_audit.RUNTIME
        }
        picture = config_audit.audit(complete)
        self.assertEqual(picture["status"], "ok")
        self.assertEqual(picture["attention"], [])
        self.assertNotIn("\u26a0", "\n".join(config_audit.boot_lines(complete)))

    def test_a_host_that_supplies_the_answer_is_not_reported_as_missing(self):
        """``DASHBOARD_HTTPS`` and ``TRUST_PROXY_HEADERS`` are *inferred*.

        ``keep_alive`` reads each from the environment *or* works it out from
        ``RENDER``/``RAILWAY_ENVIRONMENT``, so on Render both are absent and both
        are correct. Reporting them there would be two false alarms on every boot
        of the production host — the noise that makes a report ignorable.
        """
        on_host = config_audit.audit({"RENDER": "true"})
        reported = {setting.name for setting in on_host["attention"]}
        self.assertNotIn("DASHBOARD_HTTPS", reported)
        self.assertNotIn("TRUST_PROXY_HEADERS", reported)

        # And the same absences *are* findings where nothing supplies them.
        self_hosted = config_audit.audit({})
        reported = {setting.name for setting in self_hosted["attention"]}
        self.assertIn("DASHBOARD_HTTPS", reported)
        self.assertIn("TRUST_PROXY_HEADERS", reported)

    def test_the_legacy_alias_is_never_a_finding_on_its_own(self):
        """One of ``MARKET_ROLE_IDS`` / ``MARKET_ROLE_ID`` is always unset.

        The gate is a supported name and a retired spelling of it, so a report
        that flags whichever one is missing would flag every deployment that
        configured the gate correctly — including a complete one.
        """
        for environ in ({}, {"MARKET_ROLE_IDS": "123"}, {"MARKET_ROLE_ID": "123"}):
            reported = {s.name for s in config_audit.audit(environ)["attention"]}
            self.assertNotIn("MARKET_ROLE_ID", reported, f"cries wolf for {environ}")
        self.assertIn("MARKET_ROLE_IDS", {s.name for s in config_audit.audit({})["attention"]})

    def test_the_summary_sentence_counts_what_it_says_it_counts(self):
        picture = config_audit.audit({})
        line = config_audit.summary_line({})
        self.assertIn(str(picture["counts"]["unset"]), line)
        self.assertIn(str(len(picture["attention"])), line)


class NothingIsEchoed(unittest.TestCase):
    """Names and booleans only. Never a value — and that must not be incidental."""

    #: A value that appears nowhere else, so finding it in a response is proof.
    SENTINEL = "sentinel-value-that-must-never-be-echoed"

    def setUp(self):
        self.environ = {setting.name: self.SENTINEL for setting in config_audit.SETTINGS}
        self.passkey = self.SENTINEL
        patcher = mock.patch.dict(os.environ, self.environ, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        keep_alive.ADMIN_PASSKEY = self.passkey
        self.addCleanup(setattr, keep_alive, "ADMIN_PASSKEY", keep_alive.ADMIN_PASSKEY)
        self.client = keep_alive.app.test_client()

    def test_health_names_nothing(self):
        """``/health`` is unauthenticated: it counts, and does not name."""
        body = self.client.get("/health").get_json()
        blob = json.dumps(body)
        for setting in config_audit.SETTINGS:
            self.assertNotIn(setting.name, blob, "/health named a setting")
        self.assertIn("config", body)

    def test_health_does_not_report_the_bot_as_unhealthy(self):
        """Configured-ness and health are different claims about different things.

        A monitor that has read ``status`` since before this field existed must not
        start seeing a bot that is down because nobody pasted a webhook URL.
        """
        body = self.client.get("/health").get_json()
        self.assertEqual(body["status"], "ok")

    def test_the_operator_view_carries_no_value_at_all(self):
        response = self.client.get("/api/config", headers={"X-Admin-Passkey": self.passkey})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.SENTINEL, response.get_data(as_text=True))
        body = response.get_json()
        self.assertEqual(len(body["settings"]), len(config_audit.SETTINGS))
        for row in body["settings"]:
            self.assertIsInstance(row["set"], bool)
            self.assertNotIn(self.SENTINEL, json.dumps(row))

    def test_a_secret_is_marked_as_one(self):
        """The flag is what tells a future reader not to paste a value in."""
        secrets = {s.name for s in config_audit.SETTINGS if s.secret}
        for expected in ("DISCORD_TOKEN", "SUPABASE_SERVICE_KEY", "ADMIN_PASSKEY",
                         "FLASK_SECRET_KEY", "DISCORD_CLIENT_SECRET"):
            self.assertIn(expected, secrets)

    def test_the_view_is_refused_without_an_administrator(self):
        self.assertEqual(self.client.get("/api/config").status_code, 401)

    def test_a_guild_scoped_session_is_refused(self):
        """This describes the deployment, not one server."""
        client = keep_alive.app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["auth_scope"] = "guild"
        self.assertEqual(client.get("/api/config").status_code, 403)

    def test_the_one_time_link_is_enough(self):
        """Which is the point: ``ADMIN_PASSKEY`` unset is a finding this reports.

        Requiring the passkey would hide that answer behind the question it
        answers.
        """
        client = keep_alive.app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["auth_scope"] = "global"
        self.assertEqual(client.get("/api/config").status_code, 200)


if __name__ == "__main__":
    unittest.main()
