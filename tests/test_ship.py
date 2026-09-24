"""The local ship command, held to the claim it makes.

``scripts/ship.py`` exists because the workflow that was supposed to do this does
not run: every job on this repository's GitHub account is refused before it
starts, so the only path that has ever shipped a commit is somebody typing
``wrangler pages deploy`` by hand. A hand-typed procedure is worth scripting only
if the script keeps the two promises the typing kept getting wrong, and those are
what is pinned here:

1. **The hash on the deployment means something.** A build from a dirty tree, or
   from a commit that is not on the remote, produces a deployment whose commit
   hash does not name its contents — a small, ordinary kind of lie, and the same
   one this project has four laws about at other layers.
2. **An accepted upload is not a published site.** ``wrangler`` exiting 0 means
   Cloudflare took the files. The site once served a commit four behind while the
   workflow that uploaded it reported success, so the run is not finished until
   the API says which commit production carries *and* the host answers with the
   bytes this build produced.

The commands are compared against ``.github/workflows/site.yml`` in one test, on
purpose: the workflow and this script build the same directories, and two places
that must agree are exactly where this repository keeps finding drift.

Two more things are pinned here, and both of them are about a host not being
checked. **The developer hub has a publish path in this file because it had none
anywhere else**: no workflow built ``_hub``, nothing uploaded it and nothing asked
it what it served, so it sat an entire rebrand behind — a violet palette and a 404
announcing the retired name on a public address — while the other two hosts moved.
And **the machine is checked too, as the machine**: the Hugging Face Space is
pushed to rather than uploaded and carries no commit stamp, so it is asked the one
question that is available to it — whether it is up and wearing this release's
name — rather than being given the Pages hosts' check with a different URL.
"""
import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from scripts import ship
from utils import brand

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "site.yml"

#: The commit under test. Any hex string would do; this one is short enough to
#: read in a failure message and long enough to exercise the short-hash compare.
SHA = "75e32e4b48f2777803f8b04fc62223e99dd718f1"


class _Result:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _Wrangler:
    """A stand-in for ``ship.run`` that answers like the real commands do.

    It records every argv, answers a deployment list with the commit it was told
    production is serving, and reports success for everything else — so a test
    can watch the *whole* command sequence rather than one call at a time.
    """

    def __init__(self, production: str | None = SHA, list_fails: bool = False):
        self.production = production
        self.list_fails = list_fails
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if "deployment" in cmd and "list" in cmd:
            if self.list_fails:
                return _Result(stderr="wrangler blew up", returncode=1)
            rows = [{"Environment": "Preview", "Source": "0000000"}]
            if self.production is not None:
                rows.append({"Environment": "Production", "Source": self.production})
            return _Result(stdout=json.dumps(rows))
        return _Result()

    def deploys(self) -> list[list[str]]:
        return [cmd for cmd in self.calls if "deploy" in cmd]


class TheHashOnTheDeploymentMeansSomething(unittest.TestCase):
    def test_a_dirty_tracked_tree_is_refused(self):
        """Fifty files uploaded under a hash that contains none of them is how a
        deployment becomes unattributable, and it is one `git status` away."""
        with mock.patch.object(ship, "head", return_value=SHA), \
             mock.patch.object(ship, "dirty_tracked", return_value=["static/meow.css"]):
            with self.assertRaises(ship.Refused) as refusal:
                ship.preflight()
        self.assertIn("static/meow.css", str(refusal.exception))
        self.assertIn(SHA[:7], str(refusal.exception))

    def test_a_commit_no_remote_has_is_refused(self):
        """A hash nobody else can resolve names nothing, so the deployment would
        claim a provenance it does not have."""
        with mock.patch.object(ship, "head", return_value=SHA), \
             mock.patch.object(ship, "dirty_tracked", return_value=[]), \
             mock.patch.object(ship, "upstream", return_value=("origin/main", 2)):
            with self.assertRaises(ship.Refused) as refusal:
                ship.preflight()
        self.assertIn("origin/main", str(refusal.exception))
        self.assertIn("2", str(refusal.exception))

    def test_a_branch_with_no_upstream_is_refused(self):
        with mock.patch.object(ship, "head", return_value=SHA), \
             mock.patch.object(ship, "dirty_tracked", return_value=[]), \
             mock.patch.object(ship, "upstream",
                               side_effect=ship.Refused("no upstream")):
            with self.assertRaises(ship.Refused):
                ship.preflight()

    def test_a_clean_pushed_tree_is_what_gets_built(self):
        with mock.patch.object(ship, "head", return_value=SHA), \
             mock.patch.object(ship, "dirty_tracked", return_value=[]), \
             mock.patch.object(ship, "upstream", return_value=("origin/main", 0)):
            self.assertEqual(ship.preflight(), SHA)

    def test_a_dry_run_does_not_need_the_push(self):
        """Nothing is stamped, so nothing is claimed: a build to look at is the
        whole point of the flag."""
        with mock.patch.object(ship, "head", return_value=SHA), \
             mock.patch.object(ship, "dirty_tracked", return_value=[]), \
             mock.patch.object(ship, "upstream", side_effect=AssertionError("asked")):
            self.assertEqual(ship.preflight(require_push=False), SHA)

    def test_a_verification_does_not_need_a_clean_tree(self):
        """Nothing is built or stamped, so which files are open in an editor has
        no bearing on whether a published commit is being served."""
        with mock.patch.object(ship, "head", return_value=SHA), \
             mock.patch.object(ship, "dirty_tracked", side_effect=AssertionError("asked")), \
             mock.patch.object(ship, "upstream", return_value=("origin/main", 0)):
            self.assertEqual(ship.preflight(require_clean=False), SHA)

    def test_a_refusal_uploads_nothing(self):
        """Driven through ``main`` rather than ``ship``: the refusal has to reach
        the exit code, and the exit code is what a shell decides on."""
        wrangler = _Wrangler()
        with mock.patch.object(ship, "head", return_value=SHA), \
             mock.patch.object(ship, "dirty_tracked", return_value=["console/src/App.tsx"]), \
             mock.patch.object(ship, "run", wrangler), \
             contextlib.redirect_stderr(io.StringIO()) as complaints:
            code = ship.main([])
        self.assertEqual(code, 2)
        self.assertEqual(wrangler.deploys(), [], "a refused ship uploaded something")
        self.assertIn("console/src/App.tsx", complaints.getvalue())


#: The machine's own check, stubbed out. It is a real HTTP request to a real
#: address, so every test that drives a whole run says what it answered — the
#: alternative is a suite that passes or fails on whether Hugging Face is awake.
GATEWAY_UP: tuple[bool, str] = (
    True,
    f"answers as {brand.GATEWAY_SERVICE} · it carries no commit stamp, so this is "
    "that it is up and wearing this name rather than that it is this commit",
)


class EveryHostCarriesTheCommitItWasBuiltFrom(unittest.TestCase):
    def _ship(self, **kwargs):
        wrangler = _Wrangler(**kwargs)
        with mock.patch.object(ship, "head", return_value=SHA), \
             mock.patch.object(ship, "dirty_tracked", return_value=[]), \
             mock.patch.object(ship, "upstream", return_value=("origin/main", 0)), \
             mock.patch.object(ship, "run", wrangler), \
             mock.patch.object(ship, "asset_matches_build",
                               return_value=(True, "static/meow.css is this build's (0c595c784ac5)")), \
             mock.patch.object(ship, "verify_gateway", return_value=GATEWAY_UP), \
             mock.patch.object(ship, "ATTEMPTS", 2), \
             mock.patch.object(ship, "PAUSE_SECONDS", 0):
            return ship.ship(ship.HOSTS), wrangler, ship.HOSTS

    def test_every_upload_names_the_commit_the_branch_and_the_directory(self):
        code, wrangler, hosts = self._ship()
        self.assertEqual(code, 0)
        self.assertEqual(len(wrangler.deploys()), len(hosts))
        for host, command in zip(hosts, wrangler.deploys()):
            self.assertIn(f"--commit-hash={SHA}", command)
            self.assertIn(f"--project-name={host.project}", command)
            # ``--branch=main`` is what makes it a production deployment; without
            # it the upload is a preview URL that changes nothing anybody sees.
            self.assertIn("--branch=main", command)
            self.assertIn(host.out, command)
            self.assertTrue(os.path.isdir(os.path.join(ship.ROOT, host.out)),
                            "the directory uploaded is not the one that was built")

    def test_the_builders_are_the_ones_the_workflow_runs(self):
        """Where two places build one directory, they have to agree.

        Read as text rather than parsed, for the same reason
        ``tests/test_site_workflow.py`` reads it that way: PyYAML is not a
        dependency of this project.

        Only the hosts the workflow mentions are compared, and that is not the
        check weakening: the hub is deliberately *not* in the workflow (it has no
        job there, which is how it fell a rebrand behind), so the agreement that
        matters is between the two the workflow does build — and if a hub job is
        ever added, this test picks it up on the next run rather than failing on
        the fact that it exists.
        """
        workflow = WORKFLOW.read_text(encoding="utf-8")
        shared = [host for host in ship.HOSTS if host.builder[0] in workflow]
        self.assertEqual({host.project for host in shared}, {"meow-bot", "meow-owner"})
        for host in shared:
            self.assertIn(" ".join(host.builder), workflow,
                          f"{host.project} is built differently by the workflow")

    def test_a_host_serving_an_older_commit_fails_the_run(self):
        """The four-commits-behind-a-green-build failure, in one assertion."""
        code, _wrangler, _hosts = self._ship(production="f21cff6")
        self.assertEqual(code, 1)

    def test_a_host_that_answers_with_other_bytes_fails_the_run(self):
        """Cloudflare can report the right commit while the address serves
        something else — a stale upload, or a deployment that never took."""
        wrangler = _Wrangler()
        with mock.patch.object(ship, "head", return_value=SHA), \
             mock.patch.object(ship, "dirty_tracked", return_value=[]), \
             mock.patch.object(ship, "upstream", return_value=("origin/main", 0)), \
             mock.patch.object(ship, "run", wrangler), \
             mock.patch.object(ship, "asset_matches_build",
                               return_value=(False, "answered with other bytes")), \
             mock.patch.object(ship, "verify_gateway", return_value=GATEWAY_UP), \
             mock.patch.object(ship, "ATTEMPTS", 2), \
             mock.patch.object(ship, "PAUSE_SECONDS", 0):
            self.assertEqual(ship.ship(ship.HOSTS), 1)

    def test_a_project_with_no_production_deployment_is_not_a_pass(self):
        code, _wrangler, _hosts = self._ship(production=None)
        self.assertEqual(code, 1)

    def test_wrangler_failing_to_list_is_not_a_pass(self):
        code, _wrangler, _hosts = self._ship(list_fails=True)
        self.assertEqual(code, 1)

    def test_a_verification_builds_nothing_and_uploads_nothing(self):
        """The public host publishes on push by itself, so the common case is one
        command that only asks whether the push arrived."""
        wrangler = _Wrangler()
        with mock.patch.object(ship, "head", return_value=SHA), \
             mock.patch.object(ship, "dirty_tracked", return_value=["scripts/ship.py"]), \
             mock.patch.object(ship, "upstream", return_value=("origin/main", 0)), \
             mock.patch.object(ship, "run", wrangler), \
             mock.patch.object(ship, "asset_matches_build",
                               return_value=(True, "static/meow.css is this build's (0c595c784ac5)")), \
             mock.patch.object(ship, "verify_gateway", return_value=GATEWAY_UP), \
             mock.patch.object(ship, "ATTEMPTS", 2), \
             mock.patch.object(ship, "PAUSE_SECONDS", 0):
            code = ship.ship(ship.HOSTS, verify_only=True)
        self.assertEqual(code, 0)
        self.assertEqual(wrangler.deploys(), [])
        self.assertEqual([cmd for cmd in wrangler.calls
                          if any(part.endswith(".py") for part in cmd)], [])

    def test_a_verification_asks_once_rather_than_waiting(self):
        """Nothing is in flight in a verification run, so a retry is a delay and
        then the same answer — and a warning that arrives thirty seconds late is
        one the operator learns to ignore."""
        wrangler = _Wrangler(production="f21cff6")
        with mock.patch.object(ship, "head", return_value=SHA), \
             mock.patch.object(ship, "dirty_tracked", return_value=[]), \
             mock.patch.object(ship, "upstream", return_value=("origin/main", 0)), \
             mock.patch.object(ship, "run", wrangler), \
             mock.patch.object(ship, "verify_gateway", return_value=GATEWAY_UP), \
             mock.patch.object(ship, "ATTEMPTS", 4), \
             mock.patch.object(ship, "PAUSE_SECONDS", 30), \
             contextlib.redirect_stderr(io.StringIO()) as complaints:
            code = ship.ship(ship.HOSTS, verify_only=True)
        self.assertEqual(code, 1)
        asks = [cmd for cmd in wrangler.calls if "list" in cmd]
        self.assertEqual(len(asks), len(ship.HOSTS), "a verification run waited and asked again")
        self.assertIn("has simply not", complaints.getvalue())
        self.assertNotIn("accepted upload", complaints.getvalue(),
                         "a verification reported an upload that never happened")

    def test_a_verification_still_needs_the_commit_on_the_remote(self):
        """A commit nobody else has cannot have been published by anybody."""
        wrangler = _Wrangler()
        with mock.patch.object(ship, "head", return_value=SHA), \
             mock.patch.object(ship, "dirty_tracked", return_value=[]), \
             mock.patch.object(ship, "upstream", return_value=("origin/main", 1)), \
             mock.patch.object(ship, "run", wrangler), \
             contextlib.redirect_stderr(io.StringIO()) as complaints:
            code = ship.main(["--verify-only"])
        self.assertEqual(code, 2)
        self.assertIn("Push first", complaints.getvalue())
        self.assertEqual(wrangler.deploys(), [])
        self.assertEqual(wrangler.calls, [], "a refused verification asked the API anyway")

    def test_a_dry_run_builds_both_hosts_and_uploads_neither(self):
        wrangler = _Wrangler()
        with mock.patch.object(ship, "head", return_value=SHA), \
             mock.patch.object(ship, "dirty_tracked", return_value=[]), \
             mock.patch.object(ship, "run", wrangler):
            code = ship.ship(ship.HOSTS, dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(wrangler.deploys(), [])
        built = [cmd for cmd in wrangler.calls
                 if any(part.endswith(".py") for part in cmd)]
        self.assertEqual(
            [cmd[1] for cmd in built],
            [host.builder[0] for host in ship.HOSTS],
            "a host was not built",
        )


class TheProbeCannotBeAnsweredFromACache(unittest.TestCase):
    def test_the_probe_is_not_the_bare_path(self):
        """``_headers`` gives ``/static/*`` a one-year immutable cache, so the
        bare path is a key Cloudflare may answer from last year's copy — and a
        check a cache can satisfy is not a check."""
        host = ship.HOSTS[0]
        url = ship.probe_url(host, SHA)
        self.assertIn(f"{host.url}/{ship.PROBE}?", url)
        self.assertIn(SHA[:12], url)

    def test_a_served_file_that_differs_is_reported_with_both_hashes(self):
        host = ship.HOSTS[0]
        with mock.patch.object(ship, "served_bytes",
                               return_value=(b"not the sheet", "")):
            matched, note = ship.asset_matches_build(host, SHA)
        self.assertFalse(matched)
        self.assertIn("other bytes", note)
        self.assertIn(ship.PROBE, note)

    def test_an_address_that_does_not_answer_is_reported_with_the_reason(self):
        host = ship.HOSTS[0]
        with mock.patch.object(ship, "served_bytes", return_value=(None, "HTTP 404")):
            matched, note = ship.asset_matches_build(host, SHA)
        self.assertFalse(matched)
        self.assertIn("HTTP 404", note)


class TheDeploymentListIsReadAsWranglerWritesIt(unittest.TestCase):
    """Wrangler renders its table as data with capitalised keys, and which keys
    exist is wrangler's business rather than this script's — so the reader is
    defensive and these are the two shapes it has to survive."""

    def _rows(self, payload):
        with mock.patch.object(ship, "run",
                               return_value=_Result(stdout=json.dumps(payload))):
            return ship.production_commit("meow-bot")

    def test_a_preview_row_is_not_production(self):
        self.assertEqual(
            self._rows([
                {"Environment": "Preview", "Source": "0000000"},
                {"Environment": "Production", "Source": "75e32e4"},
            ]),
            "75e32e4",
        )

    def test_lowercase_keys_and_a_commit_hash_column_are_understood(self):
        self.assertEqual(
            self._rows([{"environment": "production", "commit_hash": "75e32e4"}]),
            "75e32e4",
        )

    def test_only_previews_means_no_answer(self):
        self.assertIsNone(self._rows([{"Environment": "Preview", "Source": "0000000"}]))

    def test_a_production_row_with_no_commit_is_no_answer(self):
        """A row that names no commit must not be read as *this* commit."""
        self.assertIsNone(self._rows([{"Environment": "Production"}]))

    def test_unreadable_json_is_no_answer(self):
        with mock.patch.object(ship, "run", return_value=_Result(stdout="not json")):
            self.assertIsNone(ship.production_commit("meow-bot"))


class OnlyTheHostsThisRepositoryHas(unittest.TestCase):
    def test_no_flag_is_every_host(self):
        self.assertEqual([host.project for host in ship.selected([])],
                         ["meow-bot", "meow-owner", "meowdev"])

    def test_a_named_host_is_the_one_that_ships(self):
        self.assertEqual([host.project for host in ship.selected(["meow-owner"])],
                         ["meow-owner"])

    def test_a_name_that_is_not_a_host_is_refused_with_the_list(self):
        with self.assertRaises(ship.Refused) as refusal:
            ship.selected(["meow-web"])
        self.assertIn("meow-bot", str(refusal.exception))
        self.assertIn("meow-owner", str(refusal.exception))

    def test_the_projects_are_the_ones_the_sites_announce(self):
        """The address each host verifies against is the project it deploys."""
        for host in ship.HOSTS:
            self.assertEqual(host.url, f"https://{host.project}.pages.dev")


class TheHubHasAPublishPathHere(unittest.TestCase):
    """The hub shipped by nothing, and that is exactly how it fell behind.

    ``Site`` builds ``_site`` and ``_owner``; no workflow builds ``_hub``, and no
    release path mentioned it — so a rebrand reached two hosts of three, and the
    third kept serving a violet palette and a 404 announcing the retired name on
    a public address with nothing anywhere to say so.
    """

    def hub(self) -> ship.Host:
        return next(host for host in ship.HOSTS if host.project == "meowdev")

    def test_the_hub_is_built_and_uploaded_by_this_script(self):
        hub = self.hub()
        self.assertEqual(hub.out, "_hub")
        self.assertEqual(hub.builder, ("scripts/build_hub.py", "--out", "_hub"))
        self.assertEqual(hub.url, "https://meowdev.pages.dev")

    def test_the_hub_is_a_directory_a_builder_actually_fills(self):
        """The upload's directory is the build's directory, and it has to exist."""
        self.assertTrue(os.path.isfile(os.path.join(ship.ROOT, "scripts", "build_hub.py")))
        self.assertTrue(
            os.path.isdir(self.hub().directory),
            "_hub is not built, so `wrangler pages deploy _hub` would upload a directory "
            "that is not there",
        )

    def test_the_hub_is_asked_for_a_file_only_the_hub_has(self):
        """A probe has to be a file *that* host serves.

        ``static/meow.css`` is the site's sheet: the hub's build copies only its
        own assets, so asking the hub for the site's would report *the build has no
        static/meow.css* on every run — a red release check with nothing behind it,
        which is how a check stops being read.
        """
        hub = self.hub()
        self.assertEqual(hub.probe, "static/hub.css")
        self.assertNotEqual(hub.probe, ship.PROBE)

    def test_every_probe_is_a_file_its_own_builder_starts_from(self):
        """A probe naming a file no builder produces is a check that cannot pass."""
        sources = {"meow-bot": "static", "meow-owner": "static", "meowdev": "hub/static"}
        for host in ship.HOSTS:
            source = os.path.join(ship.ROOT, sources[host.project], os.path.basename(host.probe))
            self.assertTrue(os.path.isfile(source),
                            f"{host.project}'s probe is {host.probe}, which is not a file its build has")

    def test_the_hub_probe_is_the_file_a_rebrand_moves(self):
        """And that is why this probe can see the drift the hub had: its accent is in it.

        The token is declared in ``hub/static/hub.css``, so the bytes the check
        hashes are the bytes the brand is written into — the same reason
        ``tests/test_brand.py`` holds that sheet to ``brand.ACCENT_HEX``.
        """
        sheet = (ROOT / "hub" / "static" / "hub.css").read_text(encoding="utf-8")
        self.assertIn(brand.ACCENT_HEX, sheet)


class TheMachineIsCheckedAsTheMachine(unittest.TestCase):
    """The fourth address gets the question it can answer, not the other one.

    Nothing in this file uploads the Space: ``.github/workflows/sync_hf.yml``
    force-pushes ``main`` to it and no deployment of it carries a commit stamp, so
    *which commit are you serving* is not askable. *Are you up, and wearing this
    release's name* is — its ``/health`` answers with the product's name, which is
    a fact a rebrand moves, and a machine still announcing the retired name is a
    host a whole rebrand behind.
    """

    def _payload(self, body: bytes):
        with mock.patch.object(ship, "served_bytes", return_value=(body, "")):
            return ship.verify_gateway(ship.SPACE)

    def test_the_release_report_names_all_four_addresses(self):
        """A host nobody asks is a host nobody notices is behind, four times."""
        with mock.patch.object(ship, "production_commit", return_value="75e32e4"), \
             mock.patch.object(ship, "asset_matches_build", return_value=(True, "ok")), \
             mock.patch.object(ship, "verify_gateway", return_value=GATEWAY_UP), \
             contextlib.redirect_stdout(io.StringIO()) as printed:
            code = ship.report(ship.HOSTS, SHA, wait=False)
        self.assertEqual(code, 0)
        for name in ("meow-bot", "meow-owner", "meowdev", ship.SPACE.name):
            self.assertIn(name, printed.getvalue(), f"{name} was not checked")

    def test_a_healthy_gateway_passes_and_says_what_it_did_not_prove(self):
        """The claim is smaller than the Pages hosts', and it says so."""
        ok, note = self._payload(
            json.dumps({"status": "healthy", "service": brand.GATEWAY_SERVICE}).encode()
        )
        self.assertTrue(ok)
        self.assertIn("no commit stamp", note)

    def test_a_machine_announcing_the_retired_name_fails_the_release(self):
        """Measured, not hypothetical: this is what the live Space answered."""
        ok, note = self._payload(b'{"status":"healthy","service":"meow-bot"}')
        self.assertFalse(ok)
        self.assertIn("meow-bot", note)
        self.assertIn(brand.GATEWAY_SERVICE, note)

    def test_a_machine_that_does_not_answer_names_what_updates_it(self):
        """A dead address and a stale one are different failures with one fix."""
        with mock.patch.object(ship, "served_bytes", return_value=(None, "HTTP 000")):
            ok, note = ship.verify_gateway(ship.SPACE)
        self.assertFalse(ok)
        self.assertIn("HTTP 000", note)
        self.assertIn("sync_hf.yml", note)

    def test_the_gateway_check_asks_the_address_a_monitor_watches(self):
        """The same endpoint the uptime monitor reads, so the two agree."""
        with mock.patch.object(ship, "served_bytes", return_value=(b'{}', "")) as ask:
            ship.verify_gateway(ship.SPACE)
        self.assertEqual(ask.call_args.args[0], f"{ship.SPACE.url}/health")

    def test_the_machine_is_not_a_project_this_script_uploads(self):
        """``--only`` names hosts that can be uploaded; the Space is never one of them."""
        self.assertNotIn(ship.SPACE.name, [host.project for host in ship.HOSTS])
        with self.assertRaises(ship.Refused):
            ship.selected([ship.SPACE.name])

    def test_the_machine_is_checked_even_when_only_one_host_is_named(self):
        """Which host an upload was aimed at has no bearing on what is served."""
        self.assertEqual(ship.addresses(ship.selected(["meowdev"])),
                         (ship.selected(["meowdev"])[0], ship.SPACE))


class AVerificationDoesNotClaimWhatItDidNotBuild(unittest.TestCase):
    """The byte claim needs a build of *this commit*, and a verify run has none.

    Observed on the run that shipped the hub: ``--verify-only`` reported
    ``meow-bot`` as serving other bytes than this build's, because the ``_site/``
    on disk had been built at 01:17 and the sheet changed at 01:46 — while a fresh
    build of the same commit matched the live host byte for byte (251780b583db on
    both). Cloudflare publishes the public site from its own build, so the local
    directory is not the artifact that was uploaded, and a red check with nothing
    behind it is precisely what makes the next red one unread.
    """

    def test_a_verify_only_run_does_not_compare_bytes_it_did_not_build(self):
        with mock.patch.object(ship, "production_commit", return_value=SHA[:7]), \
             mock.patch.object(ship, "asset_matches_build",
                               return_value=(False, "answered with other bytes")) as asked, \
             mock.patch.object(ship, "verify_gateway", return_value=GATEWAY_UP), \
             contextlib.redirect_stdout(io.StringIO()) as printed:
            code = ship.report(ship.HOSTS, SHA, wait=False)
        self.assertEqual(code, 0, "a stale local directory failed a verification run")
        self.assertEqual(asked.call_count, 0,
                         "a verification compared bytes it did not build")
        self.assertIn("bytes not compared", printed.getvalue())

    def test_the_note_says_what_would_make_the_claim_available(self):
        """A dropped claim has to name the command that answers it."""
        with mock.patch.object(ship, "production_commit", return_value=SHA[:7]):
            ok, note = ship.verify_host(ship.HOSTS[0], SHA, built=False)
        self.assertTrue(ok)
        self.assertIn(ship.HOSTS[0].project, note)
        self.assertIn(f"production is {SHA[:7]}", note)

    def test_a_shipping_run_still_compares_them(self):
        """The claim is dropped for a verification, not for the publish itself."""
        with mock.patch.object(ship, "production_commit", return_value="75e32e4"), \
             mock.patch.object(ship, "asset_matches_build",
                               return_value=(True, "static/meow.css is this build's (0c595c784ac5)")) as asked, \
             mock.patch.object(ship, "verify_gateway", return_value=GATEWAY_UP), \
             mock.patch.object(ship, "ATTEMPTS", 1):
            code = ship.report(ship.HOSTS, SHA, wait=True)
        self.assertEqual(code, 0)
        self.assertEqual(asked.call_count, len(ship.HOSTS))


class TheDocNamesTheSameAddresses(unittest.TestCase):
    def test_every_address_this_check_asks_is_named_in_the_deployment_doc(self):
        """A host added here and not there is the next one to be forgotten."""
        text = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
        for address in ship.addresses(ship.HOSTS):
            self.assertIn(address.name, text,
                          f"{address.name} is checked at release but not documented")


if __name__ == "__main__":
    unittest.main()
