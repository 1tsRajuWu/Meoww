"""Mirror this repository onto the Hugging Face Space that actually runs the bot.

The Space is the machine: the gateway held open, the database, the API the three
Pages hosts proxy to. It is not *uploaded to* like they are — it is the same
repository, so it is kept current by sending the tracked tree to it, and the
thing that was supposed to do that, ``.github/workflows/sync_hf.yml``, has never
run on this account: every workflow fails before a job starts (*"recent account
payments have failed or your spending limit needs to be increased"*). The only
mirror that has ever happened is somebody typing a git push by hand, which is
exactly the procedure that gets half-remembered — and it did: the Space sat
several commits behind both Pages hosts while everything reported success.

So this is that procedure, made repeatable, and it exists for the same reason
``scripts/ship.py`` does.

**It sends what a commit contains and refuses to guess.** The Space has no
deployment stamp — a pushed tree is not a release with a hash on it — so the one
thing this can be held to is that what it sends is what ``HEAD`` holds: a
modified tracked file is refused rather than mirrored, because the Space would
otherwise run code no commit contains, and a ``HEAD`` that is not on
``origin/main`` is refused, because that is the machine every other host proxies
to and nobody else could resolve the hash it is running.

**The diff is measured, not assumed, and one part of it cannot be.** The Space
reports a blob id per file, and for the files it stores as text that id *is* the
git blob id — so ``git ls-files -s`` gives a comparison that is exact and costs
nothing. For some binary files (``.ttf``, ``.ogg`` and friends) the Space keeps
its own copy and reports an id in a scheme of its own, so a mismatch there is not
evidence of drift; those are read back and compared byte for byte, which is the
only honest way to answer it. The first version of this script compared ids
alone and offered ten unchanged binary assets on every run, which is a mirror
that always reports work it did not do.

**What leaves is what the repository tracks.** The file list is ``git ls-files``
rather than a walk of the directory, so ``bot.db``, ``.env``, ``_site/`` and the
build outputs cannot be sent by accident, and a file the repository has *dropped*
is only deleted from the Space with ``--prune``: the local database the Space
runs on is not a tracked file, and a mirror that deletes whatever it does not
recognise is one that can take the machine down.

Usage (from the repo root):

    python scripts/mirror_space.py --check      # what differs; change nothing
    python scripts/mirror_space.py              # send it
    python scripts/mirror_space.py --prune      # ...and drop what the repo has

Needs ``HF_TOKEN`` in ``.env`` (or the environment). See ``scripts/README.md``.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

#: Repo root (this file lives in ``scripts/``, so go one level up).
ROOT = Path(__file__).resolve().parents[1]

#: The Space, and the one this project has always been. A name rather than an
#: argument default, because a mirror pointed at the wrong Space would send a
#: whole repository to it — and to a place whose configuration nobody set up.
DEFAULT_REPO = "1tsRajOwO/meow"


class Refused(Exception):
    """A precondition this command will not work around.

    Separate from a mirror that failed on the way, for the reason ``ship.py``
    draws the same line: this one means *nothing was sent*, and that difference
    decides what the operator does next.
    """


def run(*command: str) -> str:
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise Refused(f"{' '.join(command)} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def head() -> tuple[str, str]:
    """``HEAD``'s sha and subject, which is what the mirror commits as."""
    sha = run("git", "rev-parse", "HEAD").strip()
    subject = run("git", "log", "-1", "--format=%s").strip()
    return sha, subject


def dirty_tracked() -> list[str]:
    """Tracked files that differ from ``HEAD`` — staged or not."""
    names: list[str] = []
    for args in (("git", "diff", "--name-only"), ("git", "diff", "--cached", "--name-only")):
        names.extend(run(*args).split())
    return sorted(set(names))


def upstream() -> tuple[str, int]:
    """``origin/main``'s name and how many commits ahead of it ``HEAD`` is."""
    name = "origin/main"
    out = run("git", "rev-list", "--count", f"{name}..HEAD").strip()
    return name, int(out or 0)


def tracked() -> dict[str, str]:
    """Every tracked file, as ``path -> git blob id``.

    Ordinary text files' blob id is directly comparable with what the Space
    reports. Read from the index rather than recomputed, so this is the id the
    repository itself holds rather than one this script hashes for itself.
    """
    raw = run("git", "ls-files", "-s", "-z")
    files: dict[str, str] = {}
    for entry in raw.split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        files[path] = meta.split()[1]
    return files


def remote(api, repo: str) -> tuple[dict[str, int], dict[str, str]]:
    """The Space's files as ``path -> size`` and ``path -> blob id``."""
    sizes: dict[str, int] = {}
    blobs: dict[str, str] = {}
    for entry in api.list_repo_tree(repo, repo_type="space", recursive=True):
        if not hasattr(entry, "blob_id"):
            continue
        sizes[entry.path] = entry.size or 0
        blobs[entry.path] = entry.blob_id
    return sizes, blobs


def same_bytes(repo: str, path: str) -> bool:
    """Whether the Space's copy of ``path`` is byte-identical to this one.

    Only asked when the Space's blob id differs from the index's *and* the size
    agrees, which is either a same-length edit or — far more often — one of the
    binary types the Space stores itself. Reading it back answers the question
    the ids cannot; downloading the few files this touches costs less than
    sending them again on every run.
    """
    from huggingface_hub import hf_hub_download  # noqa: PLC0415 — only this path needs it

    there = hf_hub_download(repo, path, repo_type="space")
    with open(there, "rb") as handle:
        theirs = handle.read()
    with open(ROOT / path, "rb") as handle:
        ours = handle.read()
    return theirs == ours


def plan(api, repo: str, files: dict[str, str]) -> tuple[list[str], list[str]]:
    """What to send and what to drop, measured rather than assumed.

    A path the **index** holds but the working tree no longer has is a difference
    and not an error: that is what a deletion nobody has committed looks like
    from here, and ``--check`` exists to describe exactly this kind of tree
    (`--check` is the one mode that runs without the clean-tree refusal, so a
    ``.stat()`` on a file that is gone was a crash rather than an answer). There
    is nothing to send for it — the bytes are not on disk — so it is counted as
    a drop: the machine holds it and this tree does not. Nothing is deleted by
    the *sending* path, which refuses a tree like this one outright.
    """
    sizes, blobs = remote(api, repo)
    send: list[str] = []
    gone: set[str] = set()
    for path in sorted(files):
        local = ROOT / path
        if not local.is_file():
            gone.add(path)
            continue
        if path not in sizes:
            send.append(path)
            continue
        if sizes[path] != local.stat().st_size:
            send.append(path)
            continue
        if blobs.get(path) == files[path]:
            continue
        if same_bytes(repo, path):
            continue
        send.append(path)
    drop = sorted(path for path in sizes if path not in files or path in gone)
    return send, drop


def send(api, repo: str, paths: list[str], drop: list[str], message: str, description: str) -> str:
    """One commit carrying every file that differs, and the drops if asked."""
    from huggingface_hub import CommitOperationAdd, CommitOperationDelete  # noqa: PLC0415

    operations = [
        CommitOperationAdd(path_in_repo=path, path_or_fileobj=str(ROOT / path))
        for path in paths
    ]
    operations += [CommitOperationDelete(path_in_repo=path) for path in drop]
    info = api.create_commit(
        repo_id=repo,
        repo_type="space",
        operations=operations,
        commit_message=message,
        commit_description=description,
    )
    return getattr(info, "oid", "") or ""


def describe(sha: str, subject: str, paths: list[str], dropped: list[str]) -> tuple[str, str]:
    """The commit's message and body — what moved, and under which commit."""
    message = f"Mirror {sha[:7]}: {subject}"
    lines = [
        f"{len(paths)} file(s) from {sha}.",
        "",
        "Sent by scripts/mirror_space.py, because the workflow that used to do this",
        "has never run on this account.",
    ]
    if dropped:
        lines += ["", "Dropped (no longer tracked):", *(f"  {path}" for path in dropped)]
    return message, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"the Space to mirror into (default {DEFAULT_REPO})")
    parser.add_argument("--check", action="store_true", help="report what differs and change nothing")
    parser.add_argument("--prune", action="store_true", help="also delete files the repository no longer tracks")
    parser.add_argument("--dry-run", action="store_true", help="the same plan as --check, with the commit it would make")
    args = parser.parse_args(argv)

    sha, subject = head()
    if not args.check:
        changed = dirty_tracked()
        if changed:
            shown = ", ".join(changed[:5]) + (" …" if len(changed) > 5 else "")
            raise Refused(
                f"{len(changed)} tracked file(s) differ from {sha[:7]} ({shown}). "
                "The Space has no commit stamp, so what it runs would be code no "
                "commit contains. Commit them, or stash them, then run this again."
            )
        name, ahead = upstream()
        if ahead:
            raise Refused(
                f"HEAD is {ahead} commit(s) ahead of {name}. Push first: this is the "
                "machine every other host proxies to, and a commit nobody else has "
                "is one nobody can resolve to anything."
            )

    # The two refusals above are facts about *this* checkout — local git
    # questions, asked before anything is sent — while the token is only the
    # ability to send. A machine without one still learns what is wrong with
    # its tree, and this order is also what makes those two refusals the test
    # suite's to hold: `tests/test_mirror_space.py` exercises both without a
    # secret, a network, or `huggingface_hub` being installed at all.
    token = os.getenv("HF_TOKEN", "").strip()
    if not token and not args.check:
        print("✗ HF_TOKEN is not set, so nothing can be sent. Put it in .env.", file=sys.stderr)
        return 2

    from huggingface_hub import HfApi  # noqa: PLC0415 — keeps --help instant

    api = HfApi(token=token or None)

    files = tracked()
    paths, dropped = plan(api, args.repo, files)
    print(f"  {args.repo:<24} {len(files)} tracked files, {len(paths)} to send, {len(dropped)} untracked there")
    for path in paths[:20]:
        print(f"      send  {path}")
    if len(paths) > 20:
        print(f"      … and {len(paths) - 20} more")
    for path in dropped:
        print(f"      {'drop' if args.prune else 'left'}  {path}")
    if dropped and not args.prune:
        print("      (--prune deletes them; without it they stay where they are)")

    if args.check or args.dry_run:
        if not paths and not (dropped and args.prune):
            print("✓ The Space already holds this tree.")
            return 0
        print("✗ The Space is behind this tree." if not args.check else "· This is the plan; nothing was sent.")
        return 1 if args.check else 0

    if not paths and not (dropped and args.prune):
        print("✓ The Space already holds this tree.")
        return 0

    message, description = describe(sha, subject, paths, dropped if args.prune else [])
    print(f"  sending one commit: {message}")
    oid = send(api, args.repo, paths, dropped if args.prune else [], message, description)
    if not oid:
        print("✗ The commit was not accepted.", file=sys.stderr)
        return 1

    after = getattr(api.repo_info(args.repo, repo_type="space"), "sha", "")
    print(f"  Space head: {after[:12] or 'unknown'}  (sent {oid[:12]})")
    print(f"✓ Mirrored {sha[:7]} — {len(paths)} file(s) sent.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Refused as refusal:
        print(f"✗ {refusal}", file=sys.stderr)
        raise SystemExit(2) from None
