"""Ship every static host in one command, and check that the whole product took it.

There are three Cloudflare Pages projects behind this repository — ``meow-bot``
for the public site, ``meow-owner`` for the operator console and ``meowdev`` for
the developer hub — and all three are built here and uploaded here, because the
thing that was supposed to do it does not run. Every workflow run on this
repository's GitHub account fails before a job starts (*"The job was not started
because recent account payments have failed or your spending limit needs to be
increased"*), so ``Site`` never builds and never publishes, and the only path
that has ever actually shipped a commit is somebody typing ``wrangler pages
deploy`` by hand. Four commands, in the right order, with the right
``--commit-hash`` on each — which is exactly the kind of procedure that gets
half-remembered at the moment it matters.

**And the hub is here because it had no publish path at all.** ``Site`` has a job
for ``_site`` and one for ``_owner`` and none for ``_hub``, so the hub changed
only when somebody remembered it by hand — which is how it sat an entire rebrand
behind, still serving a violet palette and a 404 that said the retired name,
while the other two hosts moved. A host with no path in this file is a host the
next rebrand forgets, so every Pages project this repository builds now ships
from here and is checked here.

**There is a fourth address, and it is not one this script uploads.** The machine
itself — the Hugging Face Space the other three proxy to — is *pushed* to rather
than uploaded (``.github/workflows/sync_hf.yml`` force-pushes ``main``), and no
deployment of it carries a commit stamp, so the byte check that proves a Pages
host took this build has no analogue there. What it can be held to is the one
claim it makes about itself: its ``/health`` answers with this product's name, so a
machine still announcing the retired one is a host a whole rebrand behind. The
report therefore names all four addresses, and each is asked the question that is
actually available to it — never a stand-in for it.

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
site is fresh — the file it fetches is each host's own stylesheet
(``static/meow.css`` for the site and the console, ``static/hub.css`` for the
hub, where the accent token lives), and a sheet only changes when the sheet does.
The freshness claim is the first check's, and the two are reported apart so
neither is taken for the other. It also only makes that claim about a directory
*this run* built: ``--verify-only`` builds nothing, and for ``meow-bot`` the
deployment is Cloudflare's build rather than this machine's, so comparing the
host against a stale ``_site/`` would report the host as stale when it is only
the directory — a false red, which is how a warning stops being read.

Nothing here pushes: commit and push first, or use the push flow, because an
upload stamped with a hash that is not on the remote is the failure the refusals
above exist to prevent. ``--dry-run`` builds all three hosts and stops.

**One of the projects publishes without this script, and that is why
``--verify-only`` exists.** ``meow-bot`` is connected to the repository through
Cloudflare's own Git integration, so a push to ``main`` is built and published
by Cloudflare within seconds, off the repository rather than off this machine —
measured, not assumed: nine seconds after one push the production deployment
already carried it. ``meow-owner`` and ``meowdev`` have no Git integration at
all, so they change only when somebody deploys them. An upload of ``meow-bot``
from here is therefore a second, deliberate copy of the same commit, which is
worth having when the point is to publish *this* tree rather than to wait — and
worth skipping when the point is only to know whether the push arrived, which is
the usual case:

    python scripts/ship.py --verify-only

Usage::

    python scripts/ship.py                  # build, upload and verify all three
    python scripts/ship.py --dry-run        # build all three, upload nothing
    python scripts/ship.py --verify-only    # upload nothing; check the push landed
    python scripts/ship.py --only meowdev   # one host, when only it changed

``--verify-only`` asks all four addresses however narrow ``--only`` is: which host
an upload was aimed at has no bearing on what the product is serving.
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

#: ROOT before anything of this project's is imported. This file is run as
#: ``python scripts/ship.py``, which puts ``scripts/`` first on the path and not
#: the repository root, so ``utils`` would not resolve without this line.
sys.path.insert(0, ROOT)

#: One fact about this release that the machine serves rather than renders: its
#: ``/health`` names the product, so the Space can be checked against the brand
#: instead of against a string typed in two places (see `app.py`).
from utils import brand  # noqa: E402 — needs ROOT on the path first

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

#: The file a byte check fetches, relative to a build directory, when a host does
#: not name one of its own. It has to be a file that host's build writes, that host
#: actually serves, and that a change to the product moves — otherwise the check
#: either fails for a reason that is not a deployment, or passes for one that is.
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
    #: The file *this* host answers the byte check with. Per-host rather than one
    #: shared constant, because a host only serves its own build: asking the hub
    #: for the site's sheet would report *the build has no static/meow.css* on
    #: every run — a red check for a reason that has nothing to do with a deploy.
    probe: str = PROBE

    @property
    def directory(self) -> str:
        return os.path.join(ROOT, self.out)

    @property
    def name(self) -> str:
        """What the report calls this address."""
        return self.project


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
    Host(
        project="meowdev",
        out="_hub",
        builder=("scripts/build_hub.py", "--out", "_hub"),
        url="https://meowdev.pages.dev",
        # Its own sheet, and precisely the file a rebrand moves: the accent token
        # is declared in `hub/static/hub.css`, so the bytes hashed here are the
        # bytes the brand is written into. That is what makes the hub's staleness
        # — a whole rebrand of it, unnoticed — a thing this check can see.
        probe="static/hub.css",
    ),
)


@dataclasses.dataclass(frozen=True)
class Space:
    """The machine, and the one claim that can be made about it.

    This is the address the three Pages hosts proxy to: the Discord gateway, the
    database and the API. Nothing in this file builds or uploads it —
    ``.github/workflows/sync_hf.yml`` force-pushes ``main`` to it — and no
    deployment of it carries a commit stamp, so *which commit is it serving* is
    not a question this script can answer, let alone fail on.

    What it does answer is ``/health``, whose ``service`` field is this product's
    name. That is a fact a rebrand moves, served by the machine itself, and a
    Space still announcing the name the product stopped using is a host a whole
    rebrand behind — so this is the question it gets asked, and the report says
    in words which claim passed rather than lending it the Pages hosts'.
    """

    name: str
    url: str
    health: str = "/health"


#: The machine. Named here so the release report can name it too: a product
#: checked at three addresses and deployed at four has one host nobody looks at.
SPACE = Space(name="1tsrajowo-meow.hf.space", url="https://1tsrajowo-meow.hf.space")


def addresses(hosts: tuple[Host, ...]) -> tuple[Host | Space, ...]:
    """Every address a release is checked against: the ones built here, and the machine."""
    return (*hosts, SPACE)


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


def preflight(*, require_clean: bool = True, require_push: bool = True) -> str:
    """Everything that has to be true before a byte is uploaded, and the sha.

    ``require_clean`` is off for a verification-only run, which uploads nothing
    and therefore stamps nothing: which files happen to be open in an editor has
    no bearing on whether a host is serving a commit that is already published.
    """
    sha = head()
    if require_clean:
        dirty = dirty_tracked()
        if dirty:
            shown = ", ".join(dirty[:5]) + (" …" if len(dirty) > 5 else "")
            raise Refused(
                f"{len(dirty)} tracked file(s) differ from {sha[:7]} ({shown}). A "
                "build from this tree would be uploaded under a hash that does not "
                "contain it. Commit them, or stash them, then run this again."
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
    return f"{host.url}/{host.probe}?ship={sha[:12]}"


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
    path = os.path.join(host.directory, host.probe)
    try:
        with open(path, "rb") as handle:
            built = handle.read()
    except OSError as error:
        return False, f"the build has no {host.probe}: {error}"
    live, note = served_bytes(probe_url(host, sha))
    if live is None:
        return False, f"{probe_url(host, sha)} did not answer ({note})"
    expected, found = hashlib.sha256(built).hexdigest(), hashlib.sha256(live).hexdigest()
    if expected != found:
        return False, (
            f"{host.url}/{host.probe} answered with other bytes than this build's "
            f"(host {found[:12]}, build {expected[:12]})"
        )
    return True, f"{host.probe} is this build's ({found[:12]})"


def gateway_payload(space: Space) -> tuple[dict | None, str]:
    """``(health payload, note)`` for the machine — the note says what it answered instead."""
    body, note = served_bytes(f"{space.url}{space.health}")
    if body is None:
        return None, note
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None, f"answered {body[:60]!r}, which is not JSON"
    if not isinstance(payload, dict):
        return None, f"answered {body[:60]!r}, which is not a health payload"
    return payload, ""


def verify_gateway(space: Space) -> tuple[bool, str]:
    """Whether the machine answers, and answers as this product.

    Deliberately *not* the Pages hosts' check with a different URL. Those two
    claims — Cloudflare says production is this commit, and the host serves this
    build's bytes — are about an upload, and nothing here uploads the Space. The
    available claim is smaller and is reported as what it is: the machine is up,
    and it is wearing this release's name.
    """
    payload, note = gateway_payload(space)
    if payload is None:
        return False, (
            f"{space.url}{space.health} answered nothing ({note}); nothing here "
            "uploads this host — .github/workflows/sync_hf.yml pushes to it — so a "
            "machine that is not answering is one that push has not reached"
        )
    status = str(payload.get("status") or "")
    served = str(payload.get("service") or "")
    if status != "healthy" or served != brand.GATEWAY_SERVICE:
        return False, (
            f"answers {status or 'no status'} as {served or 'nothing'}, where this "
            f"release answers healthy as {brand.GATEWAY_SERVICE} — the machine is "
            "serving a build from before that name, or is not this product"
        )
    return True, (
        f"answers as {served} · it carries no commit stamp, so this is that it is "
        "up and wearing this name rather than that it is this commit"
    )


def verify(address: Host | Space, sha: str, *, built: bool) -> tuple[bool, str]:
    """The claims available about one address, reported together.

    ``built`` says whether this run produced the directory the byte check reads,
    and it is the difference between a check and a coincidence. See
    :func:`verify_host`.
    """
    if isinstance(address, Space):
        return verify_gateway(address)
    return verify_host(address, sha, built=built)


def verify_host(host: Host, sha: str, *, built: bool) -> tuple[bool, str]:
    """Both claims about one Pages host: which commit, and which bytes.

    The byte claim is only made when *this run* built the directory it reads. A
    ``--verify-only`` run builds nothing, so for ``meow-bot`` — which Cloudflare
    publishes from its own build, not from anything here — the comparison would be
    against whatever a previous run left in ``_site/``, and a directory older than
    the commit reports the deployment as stale when it is only this machine that
    is. That is not a hypothetical: it happened on the run that shipped the hub,
    where a fresh build of the same commit matched the host byte for byte while a
    stale ``_site/`` did not, and a red check with nothing behind it is how this
    file's own warnings get ignored. So the claim is dropped, in words, rather
    than answered from the wrong side.
    """
    landed = production_commit(host.project)
    if landed is None:
        return False, "Cloudflare reported no production deployment for this project"
    # A short hash is wrangler's, so the comparison is on the head of ours: the
    # two agree when what the deployment names is this commit and no other.
    if not landed.startswith(sha[:7]):
        return False, f"production is {landed}, not {sha[:7]}"
    if not built:
        return True, (
            f"production is {landed} · bytes not compared: this run built nothing, "
            "and the byte claim is about a build of this commit "
            "(`--only " + host.project + "` builds one and checks it)"
        )
    matched, note = asset_matches_build(host, sha)
    if not matched:
        return False, note
    return True, f"production is {landed} · {note}"


def ship(
    hosts: tuple[Host, ...],
    *,
    dry_run: bool = False,
    verify_only: bool = False,
) -> int:
    """Build, upload and verify. Returns the exit code."""
    sha = preflight(require_clean=not verify_only, require_push=not dry_run)
    if verify_only:
        print(
            f"Verifying {sha[:7]} on {', '.join(address.name for address in addresses(hosts))}\n"
            "(nothing will be built or uploaded, so the byte claim is not made —\n"
            " it proves a build of this commit, and `--only <project>` builds one)\n"
        )
        return report(hosts, sha, wait=False)
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
    return report(hosts, sha, wait=True)


def report(hosts: tuple[Host, ...], sha: str, *, wait: bool) -> int:
    """Ask each host what commit it serves, and whether it serves this build.

    ``wait`` is the difference between the two runs and it is not a detail. After
    an upload there are two facts in flight — the alias moves when the deployment
    finishes, and the byte check goes through a cache — so the question is asked
    again for half a minute before it is read as an answer; a verification that
    fails a half-second after an upload teaches the operator to ignore it. A
    ``--verify-only`` run has nothing in flight: nothing will change while it
    sleeps, so it asks once and reports what it found.
    """
    attempts = ATTEMPTS if wait else 1
    checked = addresses(hosts)
    # Aligned to the longest address rather than to a number: the machine's name
    # is twice the length of the longest Pages project, and a column that misses
    # its widest entry is a report nobody reads down.
    width = max(len(address.name) for address in checked) + 2
    unmet: list[str] = []
    for attempt in range(1, attempts + 1):
        unmet = []
        for address in checked:
            ok, note = verify(address, sha, built=wait)
            mark = "✓" if ok else "✗"
            print(f"  {mark} {address.name:<{width}} {note}")
            if not ok:
                unmet.append(f"{address.name}: {note}")
        if not unmet:
            verb = "Shipped" if wait else "Confirmed"
            print(f"\n{verb} {sha[:7]} on {len(checked)} addresses, and every one answered.")
            return 0
        if attempt < attempts:
            print(f"    … not yet ({attempt}/{attempts}); asking again in {PAUSE_SECONDS}s\n")
            time.sleep(PAUSE_SECONDS)

    if wait:
        advice = (
            "\n\nThis is the gap between an accepted upload and a published site, and it"
            "\nmeans the live address is still an older commit. Check the deployment in"
            "\nthe Cloudflare dashboard before trusting the URL."
        )
    else:
        # Nothing was uploaded here, so *unpublished* is the ordinary reading
        # rather than an alarm: `meow-owner` and `meowdev` have no Git
        # integration, so they change only when somebody deploys them, and the
        # public host's build is Cloudflare's rather than this machine's.
        advice = (
            "\n\nNothing in this run uploaded anything, so this is the state of the"
            "\naddresses rather than a failed publish. A Pages host that is behind"
            "\nhas simply not been deployed for this commit:"
            "\n`python scripts/ship.py --only <project>`. The machine is a push rather"
            "\nthan an upload — updated by `.github/workflows/sync_hf.yml`, or by hand —"
            "\nso a Space that is behind is one that push has not reached."
        )
    print(
        "\n✗ Not every address is serving this release:\n  "
        + "\n  ".join(unmet)
        + advice,
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
        help=(
            "one host instead of all three "
            f"({', '.join(h.project for h in HOSTS)}). Every address is checked "
            "either way, the Space included"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build all three hosts and stop; upload nothing and verify nothing",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="upload nothing; check that the hosts already serve the pushed commit",
    )
    args = parser.parse_args(argv)
    try:
        hosts = selected(args.only)
        return ship(hosts, dry_run=args.dry_run, verify_only=args.verify_only)
    except Refused as refusal:
        print(f"✗ {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
