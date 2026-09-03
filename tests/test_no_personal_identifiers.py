"""Reject developer-machine identifiers in tracked text files."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Keep each exception path-specific and auditable. A future tracked benchmark
# is intentionally listed before it exists; absent entries are ignored.
# Only the files that still carry an identifier belong here. The publication
# rewrite of 2026-08-30 removed the maintainer's username, machine name, and
# home paths from history; what remains in these four is the vault directory
# name, kept deliberately as measurement provenance. An allowlist entry that no
# longer applies is not harmless — it is a standing permission for a future
# leak in that path, which is why the guard fails on stale entries.



VAULT_DIRECTORY = b"obsidian" + b"_for_xiaomi"

IDENTIFIERS = (
    b"shuto" + b"ito",
    VAULT_DIRECTORY,
    b"shutos" + b"-macbook",
    # The given name alone, with no trailing space: "Shuto," and "Shuto." and a
    # line-final "Shuto" all walked past the earlier pattern.
    b"shut" + b"o",
)

ALLOWLIST = {
    # path -> (reason, the identifiers that path may contain)
    #
    # Per pattern, not per path. A path-wide excuse lets an allowlisted file
    # carry any identifier, including one nobody reviewed — an adversarial pass
    # demonstrated exactly that by planting the username in an allowlisted file
    # and in a binary, and watching this guard accept both (2026-08-31).
    # This file has to contain what it searches for.
    "tests/test_no_personal_identifiers.py": (
        "The guard names the identifiers it looks for.",
        set(IDENTIFIERS),
    ),
}


def _tracked_paths() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    return {
        os.fsdecode(raw_path)
        for raw_path in result.stdout.split(b"\0")
        if raw_path
        and (
            (ROOT / os.fsdecode(raw_path)).exists()
            or (ROOT / os.fsdecode(raw_path)).is_symlink()
        )
    }


def _tracked_bytes(relative_path: str) -> bytes:
    path = ROOT / relative_path
    if path.is_symlink():
        return os.fsencode(os.readlink(path))
    return path.read_bytes()


def test_tracked_text_has_no_unapproved_personal_identifiers() -> None:
    tracked = _tracked_paths()
    hits: dict[str, set[bytes]] = {}

    for relative_path in sorted(tracked):
        payload = _tracked_bytes(relative_path)
        # Binaries are scanned. Skipping them was a hole an adversarial pass
        # walked through, and a repository can carry a whole second history
        # inside one: a tracked git bundle held 720 commits, the previous
        # identity, and a private key, none of it visible to a text search.
        lowered = payload.lower()
        matched = {pattern for pattern in IDENTIFIERS if pattern in lowered}
        if matched:
            hits[relative_path] = matched

    violations = {
        path: sorted(patterns - set(ALLOWLIST.get(path, ("", frozenset()))[1]))
        for path, patterns in hits.items()
        if patterns - set(ALLOWLIST.get(path, ("", frozenset()))[1])
    }
    formatted = [
        f"{path}: {pattern.decode('ascii')}"
        for path, patterns in sorted(violations.items())
        for pattern in sorted(patterns)
    ]
    assert not formatted, "tracked personal identifiers found:\n" + "\n".join(formatted)

    malformed_reasons = [
        path
        for path, (reason, _patterns) in ALLOWLIST.items()
        if not reason.strip() or "\n" in reason
    ]
    assert not malformed_reasons, (
        "allowlist reasons must be non-empty single lines: "
        + ", ".join(malformed_reasons)
    )

    stale_entries = sorted(
        path for path in ALLOWLIST if path in tracked and path not in hits
    )
    assert not stale_entries, (
        "tracked allowlist entries no longer contain an identifier: "
        + ", ".join(stale_entries)
    )


def test_history_metadata_has_no_personal_identifiers() -> None:
    """Names, addresses and messages are as public as the files are.

    Rewriting file contents does not touch commit metadata, and the check that
    was supposed to notice looked only at tracked text. Two commits reached the
    publication candidate carrying the previous author identity, because the
    rewrite fixed the past while the repository's own git config kept minting
    new commits with the old name (2026-08-31).
    """

    fields = subprocess.run(
        ["git", "log", "--all", "--format=%an%n%ae%n%cn%n%ce%n%s%n%b"],
        capture_output=True,
        check=True,
        cwd=ROOT,
    ).stdout.lower()

    found = sorted(
        pattern.decode("ascii") for pattern in IDENTIFIERS if pattern in fields
    )
    assert not found, (
        "commit metadata or messages carry personal identifiers: "
        + ", ".join(found)
    )


def test_tracked_paths_have_no_personal_identifiers() -> None:
    """A file can carry an identifier in its name and nothing in its bytes."""

    offenders = sorted(
        path
        for path in _tracked_paths()
        for pattern in IDENTIFIERS
        if pattern in path.lower().encode("utf-8")
    )
    assert not offenders, "tracked paths carry personal identifiers: " + ", ".join(
        offenders
    )


ARCHIVE_MAGIC = (b"# v2 git bundle", b"# v3 git bundle", b"PACK")


def test_no_tracked_file_is_a_git_archive() -> None:
    """A repository committed as a file is a second repository.

    One was here: a tracked bundle carrying 720 commits, a third-party signing
    key, and the previous author identity. Nothing that reads this repository as
    text could see any of it, because a bundle is compressed Git objects — the
    scan that reported it clean read straight past it, and the reviewer who
    found it opened the file instead (2026-08-31).

    Checked by magic bytes rather than by extension, since the name is the part
    an author chooses.
    """

    offenders = [
        path
        for path in sorted(_tracked_paths())
        if _tracked_bytes(path)[:16].startswith(ARCHIVE_MAGIC)
    ]
    assert not offenders, (
        "tracked files are Git archives and cannot be reviewed as text: "
        + ", ".join(offenders)
    )


def test_history_carries_no_git_archive_or_identifier() -> None:
    """The published history, not just the checkout at its tip.

    Publication exposes every reachable object. A file removed yesterday is
    still readable from the commit that carried it, so the working tree being
    clean says nothing about what a reader can fetch.
    """

    listing = subprocess.run(
        ["git", "rev-list", "--all", "--objects"],
        capture_output=True,
        check=True,
        text=True,
        cwd=ROOT,
    ).stdout.splitlines()

    archives: list[str] = []
    identifiers: list[str] = []
    for line in listing:
        sha, _, path = line.partition(" ")
        kind = subprocess.run(
            ["git", "cat-file", "-t", sha],
            capture_output=True,
            text=True,
            cwd=ROOT,
        ).stdout.strip()
        if kind != "blob":
            if path and any(
                pattern in path.lower().encode("utf-8") for pattern in IDENTIFIERS
            ):
                identifiers.append(f"{sha} path {path}")
            continue
        payload = subprocess.run(
            ["git", "cat-file", "blob", sha],
            capture_output=True,
            check=True,
            cwd=ROOT,
        ).stdout
        if payload[:16].startswith(ARCHIVE_MAGIC):
            archives.append(f"{sha} {path}")
        # The guard's own file is allowed to name what it searches for; every
        # other historical blob is not, and history has no allowlist because a
        # past excuse cannot be reviewed against a present reason.
        if path != "tests/test_no_personal_identifiers.py":
            lowered = payload.lower()
            hits = sorted(
                pattern.decode("ascii")
                for pattern in IDENTIFIERS
                if pattern in lowered and pattern != VAULT_DIRECTORY
            )
            if hits:
                identifiers.append(f"{sha} {path}: {', '.join(hits)}")

    assert not archives, "reachable history contains Git archives: " + ", ".join(
        archives
    )
    assert not identifiers, "reachable history carries identifiers: " + ", ".join(
        identifiers[:20]
    )


def test_refs_and_tags_carry_no_personal_identifiers() -> None:
    """Names of refs, and the messages inside annotated tags, publish too."""

    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)%0a%(taggername)%0a"
         "%(taggeremail)%0a%(contents)"],
        capture_output=True,
        check=True,
        cwd=ROOT,
    ).stdout.lower()
    found = sorted(
        pattern.decode("ascii") for pattern in IDENTIFIERS if pattern in refs
    )
    assert not found, "refs or tags carry personal identifiers: " + ", ".join(found)
