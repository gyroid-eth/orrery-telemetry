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
    # The given name alone. It survived a whole-history rewrite in three commit
    # messages because the replacement covered file contents only, and the
    # check meant to catch that looked for the username and the machine name
    # but never for the name by itself.
    b"shut" + b"o ",
)

ALLOWLIST = {
    # path -> (reason, the identifiers that path may contain)
    #
    # Per pattern, not per path. A path-wide excuse lets an allowlisted file
    # carry any identifier, including one nobody reviewed — an adversarial pass
    # demonstrated exactly that by planting the username in an allowlisted file
    # and in a binary, and watching this guard accept both (2026-08-31).
    "packages/agentstack_mail/fixtures/differential-expected-divergences-v2.json": (
        "The workspace is measurement provenance for the accepted performance baseline.",
        {VAULT_DIRECTORY},
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
