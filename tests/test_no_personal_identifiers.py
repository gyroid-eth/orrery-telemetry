"""Reject developer-machine identifiers in tracked text files."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Keep each exception path-specific and auditable. A future tracked benchmark
# is intentionally listed before it exists; absent entries are ignored.
ALLOWLIST = {
    "bench/results.jsonl": "Measurement provenance requires the recorded host and execution path.",
    "docs/agentstack-mail-cutover-patches/0001-orrery-mail-db-selector.patch": "The patch preserves an immutable cutover before/after record.",
    "docs/agentstack-mail-cutover-patches/0002-dashboard-mail-cutover-selectors.patch": "The patch preserves an immutable cutover before/after record.",
    "docs/agentstack-mail-cutover-patches/0002b-dashboard-live-launchagent-selectors.patch": "The patch preserves an immutable cutover before/after record.",
    "docs/agentstack-mail-cutover-patches/0003-dashboard-agentstack-mail-no-bearer.patch": "The patch preserves an immutable cutover before/after record.",
    "docs/agentstack-mail-cutover-patches/0004a-dashboard-loopback-retire-exit.patch": "The patch preserves an immutable cutover before/after record.",
    "docs/agentstack-mail-cutover.md": "The runbook records exact paths and labels needed to reconstruct the handoff.",
    "packages/agentstack_mail/fixtures/differential-expected-divergences-v2.json": "The workspace is measurement provenance for the accepted performance baseline.",
    "packages/agentstack_mail/provenance/README.md": "The checkout path identifies the source captured by the provenance bundle.",
}

IDENTIFIERS = (
    b"shuto" + b"ito",
    b"obsidian" + b"_for_xiaomi",
    b"shutos" + b"-macbook",
)


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
        if b"\0" in payload:
            continue
        lowered = payload.lower()
        matched = {pattern for pattern in IDENTIFIERS if pattern in lowered}
        if matched:
            hits[relative_path] = matched

    violations = {
        path: patterns for path, patterns in hits.items() if path not in ALLOWLIST
    }
    formatted = [
        f"{path}: {pattern.decode('ascii')}"
        for path, patterns in sorted(violations.items())
        for pattern in sorted(patterns)
    ]
    assert not formatted, "tracked personal identifiers found:\n" + "\n".join(formatted)

    malformed_reasons = [
        path
        for path, reason in ALLOWLIST.items()
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
