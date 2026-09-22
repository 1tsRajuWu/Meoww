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
purpose: the workflow and this script build the same two directories, and two
places that must agree are exactly where this repository keeps finding drift.
"""
import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from scripts import ship

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


class BothHostsCarryTheCommitTheyWereBuiltFrom(unittest.TestCase):
    def _ship(self, **kwargs):
        wrangler = _Wrangler(**kwargs)
        with mock.patch.object(ship, "head", return_value=SHA), \
             mock.patch.object(ship, "dirty_tracked", return_value=[]), \
             mock.patch.object(ship, "upstream", return_value=("origin/main", 0)), \
             mock.patch.object(ship, "run", wrangler), \
             mock.patch.object(ship, "asset_matches_build",
                               return_value=(True, "static/meow.css is this build's (0c595c784ac5)")), \
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
        """Two places build these directories, and they have to agree.

        Read as text rather than parsed, for the same reason
        ``tests/test_site_workflow.py`` reads it that way: PyYAML is not a
        dependency of this project.
        """
        workflow = WORKFLOW.read_text(encoding="utf-8")
        for host in ship.HOSTS:
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
             mock.patch.object(ship, "ATTEMPTS", 2), \
             mock.patch.object(ship, "PAUSE_SECONDS", 0):
            self.assertEqual(ship.ship(ship.HOSTS), 1)

    def test_a_project_with_no_production_deployment_is_not_a_pass(self):
        code, _wrangler, _hosts = self._ship(production=None)
        self.assertEqual(code, 1)

    def test_wrangler_failing_to_list_is_not_a_pass(self):
        code, _wrangler, _hosts = self._ship(list_fails=True)
        self.assertEqual(code, 1)

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
    def test_no_flag_is_both_hosts(self):
        self.assertEqual([host.project for host in ship.selected([])],
                         ["meow-bot", "meow-owner"])

    def test_a_named_host_is_the_one_that_ships(self):
        self.assertEqual([host.project for host in ship.selected(["meow-owner"])],
                         ["meow-owner"])

    def test_a_name_that_is_not_a_host_is_refused_with_the_list(self):
        with self.assertRaises(ship.Refused) as refusal:
            ship.selected(["meow-web"])
        self.assertIn("meow-bot", str(refusal.exception))
        self.assertIn("meow-owner", str(refusal.exception))

    def test_the_projects_are_the_two_the_sites_announce(self):
        """The address each host verifies against is the project it deploys."""
        for host in ship.HOSTS:
            self.assertEqual(host.url, f"https://{host.project}.pages.dev")


if __name__ == "__main__":
    unittest.main()
