"""What this project sends out, and where — checked rather than assumed.

A security scan of this tree ("Snyk", run over the working copy) reported six
findings, and the honest reading of them was three of each kind:

* **Real, and small** — a port *probe* that bound every interface to answer a
  question about one, and a preview script that fed a command-line argument
  straight into ``exec``.
* **Real, and worth a guard rather than a fix** — two URLs that already pointed at
  the right host but were only right because somebody typed the constant that way.
  One of them carries a **bearer token** and the other carries a **licence key
  somebody paid for**, so "the URL is a constant" is a comment, and a guard that
  refuses every other host is a rule.
* **Not real** — two DOM-XSS reports in the console, where the value reaching the
  attribute is a *relative path* built by a function that refuses anything else.
  Triage is recorded here rather than in a commit message, because the next
  scanner run will report it again and the next reader deserves the argument.

The theme is narrow on purpose: every check in this file is about the boundary
between this process and somewhere else — a socket it binds, an HTTP request it
makes, a line it executes. Those are the places a defect is invisible in
development and expensive in production.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from utils import discord_auth, plans  # noqa: E402  (after the path fix)


class TheSignInRequestsGoToDiscord(unittest.TestCase):
    """A request carrying an access token may only ever reach Discord."""

    def test_the_real_endpoints_are_allowed(self):
        for url in (
            discord_auth.TOKEN_ENDPOINT,
            f"{discord_auth.API_BASE}/users/@me",
            f"{discord_auth.API_BASE}/users/@me/guilds",
        ):
            self.assertEqual(discord_auth.checked_endpoint(url), url)

    def test_anything_else_is_refused(self):
        for url in (
            "http://discord.com/api/oauth2/token",  # downgraded to plaintext
            "https://discord.com.evil.test/steal",  # a suffix that reads like a match
            "https://evil.test/discord.com",  # the name, in the path
            "https://discordapp.com/api/oauth2/token",  # a *different* host, once real
            "https://127.0.0.1:8000/token",  # this machine
            "ftp://discord.com/token",  # not HTTPS
            "https://user:pass@discord.com@evil.test/token",  # userinfo confusion
            "",
            None,
        ):
            with self.subTest(url=url):
                with self.assertRaises(discord_auth.AuthError):
                    discord_auth.checked_endpoint(url)

    def test_the_check_is_on_the_path_every_request_takes(self):
        """Not a helper somebody may forget to call.

        ``_read_json`` is the single function every outbound call in this module
        funnels through, and it checks before it builds the request. A future
        caller that adds an endpoint therefore inherits the guard instead of
        having to remember it — which is the difference between a rule and a
        comment.
        """
        import inspect

        source = inspect.getsource(discord_auth._read_json)
        self.assertIn("checked_endpoint(url)", source)
        self.assertLess(
            source.index("checked_endpoint(url)"),
            source.index("urllib.request.Request"),
            "the URL is used before it is checked",
        )


class TheLicenceRequestsGoToTheStore(unittest.TestCase):
    """A key somebody paid for may only ever be posted to the store."""

    def test_the_real_api_is_allowed(self):
        url = f"{plans.LICENSE_API}/validate"
        self.assertEqual(plans.checked_endpoint(url), url)

    def test_anything_else_is_refused(self):
        for url in (
            "http://api.lemonsqueezy.com/v1/licenses/validate",
            "https://api.lemonsqueezy.com.evil.test/v1/licenses/validate",
            "https://evil.test/v1/licenses/validate",
            "https://127.0.0.1/v1/licenses/validate",
            "",
        ):
            with self.subTest(url=url):
                with self.assertRaises(OSError):
                    plans.checked_endpoint(url)

    def test_the_check_is_on_the_path_every_call_takes(self):
        import inspect

        source = inspect.getsource(plans._post)
        self.assertIn("checked_endpoint(", source)
        self.assertLess(
            source.index("checked_endpoint("),
            source.index("urllib.request.Request"),
            "the URL is used before it is checked",
        )

    def test_a_refused_endpoint_reads_as_an_outage_not_a_refusal(self):
        """``OSError`` is the type the callers already handle.

        ``verify`` and ``activate`` catch ``OSError`` and report "could not ask
        the store", which is a *better* message for a URL that was refused than
        a refusal the buyer never received — a licence must not be deactivated
        because a request was never sent.
        """
        self.assertTrue(issubclass(OSError, Exception))
        with self.assertRaises(OSError):
            plans.checked_endpoint("https://evil.test/")


class ThePortProbeStaysOnThisMachine(unittest.TestCase):
    """``find_free_port`` asks about one interface, so it may only bind one."""

    def test_it_skips_a_port_that_is_already_taken(self):
        import socket

        import keep_alive

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            taken = held.getsockname()[1]
            found = keep_alive.find_free_port(taken)
            self.assertNotEqual(found, taken, "a port in use was reported free")
            self.assertGreater(found, taken)

    def test_it_probes_the_loopback_address(self):
        """The probe's bind address, read from the source.

        Behaviourally this is hard to assert without binding every interface,
        which is the thing under test — so the check is that no other address
        appears in the function at all.
        """
        import inspect

        import keep_alive

        source = inspect.getsource(keep_alive.find_free_port)
        self.assertIn('"127.0.0.1"', source)
        self.assertNotIn('"0.0.0.0"', source)


class TheCardPreviewRefusesToExecuteAnOption(unittest.TestCase):
    """The one argument in this project that ends in ``exec``.

    ``scripts/preview_cards.py`` loads a *revision* of ``utils/style.py`` out of
    git and executes it, so the command line reaches ``exec``. What keeps that
    safe is that the argument can only name a commit: a revision is a name, an
    option is not, and nothing that looks like one is passed through.
    """

    def _module(self):
        import importlib.util

        path = os.path.join(ROOT, "scripts", "preview_cards.py")
        spec = importlib.util.spec_from_file_location("_preview_cards_probe", path)
        module = importlib.util.module_from_spec(spec)
        # The module's own imports are all standard library; loading it is safe
        # and is the only way to reach a function whose job is validation.
        spec.loader.exec_module(module)
        return module

    def test_a_revision_is_accepted(self):
        module = self._module()
        for revision in ("HEAD", "HEAD~2", "main", "v1.2.0", "509fc61", "ab" * 20):
            self.assertEqual(module.checked_revision(revision), revision)

    def test_an_option_or_a_second_argument_is_not(self):
        module = self._module()
        for revision in (
            "--output=/tmp/pwn",
            "-o",
            "HEAD; rm -rf /",
            "HEAD utils/style.py",
            "HEAD\u2028-o",
            "'HEAD'",
            "HEAD`whoami`",
            "",
            None,
        ):
            with self.subTest(revision=revision):
                with self.assertRaises(ValueError):
                    module.checked_revision(revision)

    def test_the_revision_is_the_only_thing_git_is_asked_for(self):
        """``git show`` is called with an argument list, and with a checked name.

        An argument list (never a shell string) is what makes ``;`` and backticks
        inert; the checked name is what makes ``--output=`` inert. Both are
        needed, and this asserts the pair rather than either alone.
        """
        source = open(
            os.path.join(ROOT, "scripts", "preview_cards.py"), encoding="utf-8"
        ).read()
        self.assertIn('["git", "show", f"{checked_revision(rev)}:utils/style.py"]', source)
        self.assertNotIn("shell=True", source)

    def test_the_script_still_runs(self):
        """The guard is only worth having if the tool still works."""
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "preview_cards.py"), "--help"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--before-rev", result.stdout)


class TheConsoleIconAttribute(unittest.TestCase):
    """Two scan findings, triaged rather than "fixed" — and pinned here.

    Snyk reports DOM-based XSS twice in the console: a value from a React
    ``useState`` flowing into an ``<img src>``. The flow is real and the defect is
    not: the value is produced by ``iconSrc``, which returns **only** a
    same-origin relative path (``/…``) and refuses everything else, and React
    assigns ``src`` as a property, where a ``javascript:`` URL is inert anyway.
    Escaped by React, restricted by the function, and never interpolated into
    markup or a script — the report is the scanner being unable to *see* the
    guard, which is exactly why the guard is asserted here.
    """

    def _source(self) -> str:
        with open(
            os.path.join(ROOT, "console", "src", "lib", "format.ts"), encoding="utf-8"
        ) as handle:
            return handle.read()

    def test_only_a_relative_path_survives(self):
        source = self._source()
        self.assertIn("text.startsWith(\"/\")", source)
        self.assertIn("!text.startsWith(\"//\")", source)

    def test_every_icon_goes_through_it(self):
        """No page builds an image URL by hand."""
        pages = os.path.join(ROOT, "console", "src")
        offenders = []
        for base, _dirs, files in os.walk(pages):
            for entry in files:
                if not entry.endswith(".tsx"):
                    continue
                path = os.path.join(base, entry)
                with open(path, encoding="utf-8") as handle:
                    source = handle.read()
                for line in source.splitlines():
                    if "src={" not in line:
                        continue
                    # Allowed: the brand mark, whose path is built from the
                    # *theme id* — a value from a closed set this file owns — and
                    # which never contains a server-provided string.
                    if "iconSrc(" in line or "/static/" in line:
                        continue
                    offenders.append(f"{os.path.relpath(path, ROOT)}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "an image source that does not come from iconSrc() is an attribute a "
            "server-provided string could reach:\n" + "\n".join(offenders),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
