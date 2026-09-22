"""Ship both static hosts in one command, and check that they took it.

There are two Cloudflare Pages projects behind this repository — ``meow-bot`` for
the public site and ``meow-owner`` for the operator console — and both of them
are built here and uploaded here, because the thing that was supposed to do it
does not run. Every workflow run on this repository's GitHub account fails before
a job starts (*"The job was not started because recent account payments have
failed or your spending limit needs to be increased"*), so ``Site`` never builds
and never publishes, and the only path that has ever actually shipped a commit is
somebody typing ``wrangler pages deploy`` by hand. Four commands, in the right
order, with the right ``--commit-hash`` on each — which is exactly the kind of
procedure that gets half-remembered at the moment it matters.

So this is that procedure, with the checks that make it worth trusting:

**It refuses to stamp a lie.** A deployment carries the commit it was built from,
and wrangler will attach whatever hash it is handed. A build from a dirty tree,
or from a commit nobody else has, therefore produces a deployment whose hash does
not name its contents — the same "accepted is not published" gap this project has
written four laws about, one level down. So it refuses a modified tracked file
(the build would ship code the commit does not contain) and it refuses a ``HEAD``
that is not on ``origin/main`` (a hash no other machine, and no other person, can
resolve).

**It proves the upload landed.** ``wrangler pages deploy`` exiting 0 means
Cloudflare *accepted* the upload. It is not the same claim as "the host now
serves it", and the difference is precisely how this site sat four commits behind
a green workflow. The last step asks Cloudflare which commit each project's
newest production deployment carries, and asks the *host itself* for one file and
compares its bytes with the ones this build produced.

**And it is honest about the shape of the second claim.** What the byte check
proves is that the host is answering with this build's artifact, not that the
site is fresh — the file it fetches (``static/meow.css``, the one sheet both
hosts serve) only changes when the sheet does. The freshness claim is the first
check's, and the two are reported apart so neither is taken for the other.

Nothing here pushes: commit and push first, or use the push flow, because an
upload stamped with a hash that is not on the remote is the failure the refusals
above exist to prevent. ``--dry-run`` builds both hosts and stops.

Usage::

    python scripts/ship.py                 # build, upload and verify both hosts
    python scripts/ship.py --dry-run       # build both hosts, upload nothing
    python scripts/ship.py --only meow-bot # one host, when only it changed
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The wrangler command. ``npx --yes`` because wrangler is not a dependency of
#: this repository — it is a deploy tool, not a build tool, and the one machine
#: that has it installed for the console has an old one. A machine that *does*
#: have wrangler on its PATH can say so, and then nothing reaches the network
#: until the upload:
#:     MEOW_WRANGLER=wrangler python scripts/ship.py
WRANGLER = os.environ.get("MEOW_WRANGLER", "npx --yes wrangler").split()

#: The branch a deployment is a production deployment *of*. ``--branch=main`` is
#: what tells Cloudflare to move the production alias rather than mint a preview
#: URL, which is the single most expensive thing to get wrong here: a preview
#: upload looks like a successful deploy in every line of its own output and
#: changes nothing anybody can see.
BRANCH = "main"

#: The file the byte check fetches, relative to each build directory. It is the
#: one asset both hosts upload and both hosts serve under the same path, and it
#: is copied rather than rendered, so the bytes on the host are the bytes on
#: disk — which is what makes a comparison meaningful at all.
PROBE = "static/meow.css"

#: How long to wait for a deployment to become the one production serves. The
#: alias moves in seconds, but the second check goes through Cloudflare's cache
#: and the two do not have to happen in the same moment.
ATTEMPTS = 6
PAUSE_SECONDS = 6

#: Where production answers for each host if no verification has arrived yet.
HTTP_TIMEOUT = 30


@dataclasses.dataclass(frozen=True)
class Host:
    """One Cloudflare Pages project, the builder that fills it, and its address."""

    project: str
    out: str
    builder: tuple[str, ...]
    url: str

    @property
    def directory(self) -> str:
        return os.path.join(ROOT, self.out)


HOSTS: tuple[Host, ...] = (
    Host(
        project="meow-bot",
        out="_site",
        # ``--proxy`` keeps the dynamic URLs relative so Pages' worker can
        # reverse-proxy them; without it the built pages point at the Render
        # service by absolute URL and the worker is bypassed.
        builder=("scripts/build_site.py", "--out", "_site", "--proxy"),
        url="https://meow-bot.pages.dev",
    ),
    Host(
        project="meow-owner",
        out="_owner",
        builder=("scripts/build_owner.py", "--out", "_owner"),
        url="https://meow-owner.pages.dev",
    ),
)


class Refused(Exception):
    """A precondition this command will not work around.

    Separate from a verification failure on purpose: this one means *nothing was
    uploaded*, and the difference decides what the operator does next.
    """


def run(cmd: list[str], *, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a command in the repository root. The one process seam there is."""
    return subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        capture_output=capture,
        check=False,
    )


def git(*args: str) -> subprocess.CompletedProcess:
    return run(["git", *args])


def head() -> str:
    """The commit that would be built, at its full length."""
    result = git("rev-parse", "HEAD")
    if result.returncode != 0:
        raise Refused(f"git rev-parse HEAD failed: {result.stderr.strip()}")
    return result.stdout.strip()


def dirty_tracked() -> list[str]:
    """Tracked files that differ from ``HEAD``, staged or not.

    Untracked files are deliberately not in this list: a build directory and a
    scratch file belong to whoever is working, while a *tracked* file that is not
    in the commit is code the deployment would ship under a hash that does not
    contain it.
    """
    result = git("status", "--porcelain", "--untracked-files=no")
    if result.returncode != 0:
        raise Refused(f"git status failed: {result.stderr.strip()}")
    files = []
    for line in result.stdout.splitlines():
        if len(line) > 3:
            files.append(line[3:].strip())
    return files


def upstream() -> tuple[str, int]:
    """``(name, commits ahead)`` for this branch's upstream.

    Raises when there is no upstream at all — a detached ``HEAD`` or a branch
    that was never pushed — because then there is nothing to compare against and
    the answer is *push something* rather than *carry on*.
    """
    name = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if name.returncode != 0 or not name.stdout.strip():
        raise Refused(
            f"{BRANCH} has no upstream to compare against, so this commit is on "
            "no remote: a deployment stamped with its hash names a commit nobody "
            "else can resolve. Push the branch first."
        )
    ahead = git("rev-list", "--count", "@{upstream}..HEAD")
    if ahead.returncode != 0:
        raise Refused(f"git rev-list failed: {ahead.stderr.strip()}")
    return name.stdout.strip(), int(ahead.stdout.strip() or 0)


def preflight(*, require_push: bool = True) -> str:
    """Everything that has to be true before a byte is uploaded, and the sha."""
    sha = head()
    dirty = dirty_tracked()
    if dirty:
        shown = ", ".join(dirty[:5]) + (" …" if len(dirty) > 5 else "")
        raise Refused(
            f"{len(dirty)} tracked file(s) differ from {sha[:7]} ({shown}). A build "
            "from this tree would be uploaded under a hash that does not contain "
            "it. Commit them, or stash them, then run this again."
        )
    if not require_push:
        return sha
    name, ahead = upstream()
    if ahead:
        raise Refused(
            f"HEAD is {ahead} commit(s) ahead of {name}. Push first: the "
            "deployment carries this commit's hash, and a hash that is not on the "
            "remote is one nobody else can resolve to anything."
        )
    return sha


def build(host: Host) -> None:
    """Render one host's directory, loudly, in this interpreter."""
    command = [sys.executable, *host.builder]
    print(f"  {host.project:<12} building {host.out}  ({' '.join(host.builder)})")
    result = run(command, capture=False)
    if result.returncode != 0:
        raise Refused(f"{' '.join(command)} exited {result.returncode}")


def deploy(host: Host, sha: str) -> None:
    """Upload one build directory as a *production* deployment of this commit."""
    command = [
        *WRANGLER, "pages", "deploy", host.out,
        f"--project-name={host.project}",
        f"--branch={BRANCH}",
        f"--commit-hash={sha}",
        # A fixed string rather than a commit subject: a subject can contain a
        # quote or a newline, and it would reach a command line.
        "--commit-message=Deployed by scripts/ship.py",
    ]
    print(f"  {host.project:<12} uploading {host.out}")
    result = run(command, capture=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise Refused(f"wrangler exited {result.returncode} for {host.project}:\n{detail}")


def production_commit(project: str) -> str | None:
    """The commit the newest production deployment of a project carries.

    Cloudflare's own answer, not wrangler's exit code — the upload being accepted
    and the alias pointing at it are two different facts, and only this one is
    about what a visitor gets.

    The JSON is parsed defensively because it is wrangler's table rendered as
    data: the keys are capitalised, and which keys exist is wrangler's business
    rather than this file's.
    """
    result = run([
        *WRANGLER, "pages", "deployment", "list",
        f"--project-name={project}", "--json",
    ])
    if result.returncode != 0:
        return None
    try:
        rows = json.loads(result.stdout or "[]")
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        environment = str(row.get("Environment") or row.get("environment") or "")
        if environment.lower() != "production":
            continue
        source = row.get("Source") or row.get("source") or row.get("commit_hash")
        return str(source).strip() if source else None
    return None


def probe_url(host: Host, sha: str) -> str:
    """The address the byte check asks.

    A query of our own rather than the bare path, because ``_headers`` gives
    ``/static/*`` a one-year immutable cache: the bare path is a key a CDN is
    entitled to answer from last year's copy, and a check that can be satisfied
    by a cached file is not a check.
    """
    return f"{host.url}/{PROBE}?ship={sha[:12]}"


def served_bytes(url: str) -> tuple[bytes | None, str]:
    """``(body, note)`` for a URL — the note is why there is no body."""
    request = urllib.request.Request(
        url,
        headers={
            # ``identity`` because a compressed body would be hashed as bytes on
            # the wire rather than as the file, and compare unequal for a reason
            # that has nothing to do with the deployment.
            "Accept-Encoding": "identity",
            "User-Agent": "meow-ship-check",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.read(), ""
    except urllib.error.HTTPError as error:
        return None, f"HTTP {error.code}"
    except Exception as error:  # urllib raises several things for one failure
        return None, f"{type(error).__name__}: {error}"


def asset_matches_build(host: Host, sha: str) -> tuple[bool, str]:
    """Does the host answer the probe with the bytes this build produced?"""
    path = os.path.join(host.directory, PROBE)
    try:
        with open(path, "rb") as handle:
            built = handle.read()
    except OSError as error:
        return False, f"the build has no {PROBE}: {error}"
    live, note = served_bytes(probe_url(host, sha))
    if live is None:
        return False, f"{probe_url(host, sha)} did not answer ({note})"
    expected, found = hashlib.sha256(built).hexdigest(), hashlib.sha256(live).hexdigest()
    if expected != found:
        return False, (
            f"{host.url}/{PROBE} answered with other bytes than this build's "
            f"(host {found[:12]}, build {expected[:12]})"
        )
    return True, f"{PROBE} is this build's ({found[:12]})"


def verify(host: Host, sha: str) -> tuple[bool, str]:
    """Both claims about one host, reported together."""
    landed = production_commit(host.project)
    if landed is None:
        return False, "Cloudflare reported no production deployment for this project"
    # A short hash is wrangler's, so the comparison is on the head of ours: the
    # two agree when what the deployment names is this commit and no other.
    if not landed.startswith(sha[:7]):
        return False, f"production is {landed}, not {sha[:7]}"
    matched, note = asset_matches_build(host, sha)
    if not matched:
        return False, note
    return True, f"production is {landed} · {note}"


def ship(hosts: tuple[Host, ...], *, dry_run: bool = False) -> int:
    """Build, upload and verify. Returns the exit code."""
    sha = preflight(require_push=not dry_run)
    print(f"Shipping {sha[:7]} ({', '.join(host.project for host in hosts)})\n")

    try:
        for host in hosts:
            build(host)
        if dry_run:
            print("\nDry run: built only. Nothing was uploaded and nothing verified.")
            return 0
        print()
        for host in hosts:
            deploy(host, sha)
    except Refused as refusal:
        print(f"\n✗ {refusal}", file=sys.stderr)
        return 2

    print()
    return report(hosts, sha)


def report(hosts: tuple[Host, ...], sha: str) -> int:
    """Wait for each host to answer with this build, and say what it answered.

    Retried rather than asked once, because the two facts arrive from different
    places: the alias moves when the deployment finishes, and the byte check goes
    through a cache. A verification that runs a half-second after the upload and
    fails is worse than no verification, because it teaches the operator to
    ignore it.
    """
    unmet: list[str] = []
    for attempt in range(1, ATTEMPTS + 1):
        unmet = []
        for host in hosts:
            ok, note = verify(host, sha)
            mark = "✓" if ok else "✗"
            print(f"  {mark} {host.project:<12} {note}")
            if not ok:
                unmet.append(f"{host.project}: {note}")
        if not unmet:
            print(f"\nShipped {sha[:7]} to {len(hosts)} host(s), and every one confirmed it.")
            return 0
        if attempt < ATTEMPTS:
            print(f"    … not yet ({attempt}/{ATTEMPTS}); asking again in {PAUSE_SECONDS}s\n")
            time.sleep(PAUSE_SECONDS)

    print(
        "\n✗ The upload was accepted and the hosts are not serving it:\n  "
        + "\n  ".join(unmet)
        + "\n\nThis is the gap between an accepted upload and a published site, and it"
        "\nmeans the live address is still an older commit. Check the deployment in"
        "\nthe Cloudflare dashboard before trusting the URL.",
        file=sys.stderr,
    )
    return 1


def selected(names: list[str]) -> tuple[Host, ...]:
    if not names:
        return HOSTS
    known = {host.project: host for host in HOSTS}
    unknown = [name for name in names if name not in known]
    if unknown:
        raise Refused(
            f"no such host: {', '.join(unknown)}. The hosts are: {', '.join(known)}"
        )
    return tuple(known[name] for name in dict.fromkeys(names))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="PROJECT",
        help=f"one host instead of both ({', '.join(h.project for h in HOSTS)})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build both hosts and stop; upload nothing and verify nothing",
    )
    args = parser.parse_args(argv)
    try:
        hosts = selected(args.only)
        return ship(hosts, dry_run=args.dry_run)
    except Refused as refusal:
        print(f"✗ {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
