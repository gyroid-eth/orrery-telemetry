"""The deployed copies must not drift from the repository that owns them.

The repository is the source of truth (2026-09-01 decision). It stopped being
so in practice: the dashboard runs from `~/.claude/tools/agent-dashboard`, which
had diverged from `dashboard/` here by 4,944 lines and fifty functions, and the
product hooks under `~/.claude/hooks` had diverged by 100 to 436 lines each.
Nobody chose that. It accumulated because editing the running copy is the
shortest path to a fix, and nothing ever said the two had parted.

This test says it. It is deliberately advisory about *which* copy is right —
that is a judgement — and precise about the fact that they differ.

Skips where the deployment is absent, since a checkout on another machine has
no reason to carry one.
"""

from __future__ import annotations

# The two known divergences as of 2026-09-01, recorded so this test reports the
# state honestly instead of failing the whole suite on a debt it did not create.
# Reconciling them is scheduled work; what this file guarantees today is that
# nothing *new* joins the list quietly. Remove a name once its copy is
# reconciled — an entry that no longer drifts fails, so the list cannot rot.

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_HOOKS = Path(
    os.environ.get("AGENTSTACK_LIVE_HOOKS", Path.home() / ".claude" / "hooks")
)
LIVE_DASHBOARD = Path(
    os.environ.get(
        "AGENTSTACK_LIVE_DASHBOARD", Path.home() / ".claude" / "tools" / "agent-dashboard"
    )
)


KNOWN_DRIFT_HOOKS = {
    "check-agent-registered.sh",
    "check-file-reservation.sh",
    "cleanup-child-agent.sh",
    "mark-agent-registered.sh",
    "monitor_child_agent.sh",
    "resolve-agent-name.sh",
    "session-start-reminder.sh",
    "set-ghostty-title.sh",
    "spawn_child.sh",
    "watch_agent_mail_signals.sh",
}
KNOWN_DRIFT_DASHBOARD = {"server.py", "graph_data.py"}


def _absent(root: Path, names: set[str]) -> set[str]:
    """Names with no deployed counterpart, which cannot be said to drift."""

    return {name for name in names if not (root / name).is_file()}


def _drifted(repo_file: Path, live_file: Path) -> bool:
    if not live_file.is_file():
        return False
    return repo_file.read_bytes() != live_file.read_bytes()


@pytest.mark.skipif(not LIVE_HOOKS.is_dir(), reason="no deployed hooks on this machine")
def test_deployed_product_hooks_match_the_repository() -> None:
    """Only the hooks this repository ships. The machine's own hooks are its own."""

    drifted = {
        path.name
        for path in (REPO_ROOT / "hooks").glob("*.sh")
        if _drifted(path, LIVE_HOOKS / path.name)
    }
    assert not (drifted - KNOWN_DRIFT_HOOKS), (
        "deployed hooks newly differ from the repository that owns them: "
        + ", ".join(sorted(drifted - KNOWN_DRIFT_HOOKS))
        + " — re-install from this repository, or bring the fix back into it"
    )
    assert not (KNOWN_DRIFT_HOOKS - drifted - _absent(LIVE_HOOKS, KNOWN_DRIFT_HOOKS)), (
        "these hooks no longer drift and should leave KNOWN_DRIFT_HOOKS: "
        + ", ".join(sorted(KNOWN_DRIFT_HOOKS - drifted))
    )


@pytest.mark.skipif(
    not LIVE_DASHBOARD.is_dir(), reason="no deployed dashboard on this machine"
)
def test_deployed_dashboard_matches_the_repository() -> None:
    drifted = {
        path.name
        for path in (REPO_ROOT / "dashboard").glob("*.py")
        if _drifted(path, LIVE_DASHBOARD / path.name)
    }
    assert not (drifted - KNOWN_DRIFT_DASHBOARD), (
        "the deployed dashboard newly differs from the repository that owns it: "
        + ", ".join(sorted(drifted - KNOWN_DRIFT_DASHBOARD))
        + " — the repository is the source of truth; reconcile rather than editing "
        "the running copy"
    )
