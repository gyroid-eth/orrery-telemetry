"""Upstream-facing regression coverage for the optional Antigravity provider."""
from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "agent-start-gemini"
CHILD = ROOT / "hooks" / "spawn_gemini_child.sh"
PREREG_CHILD = ROOT / "hooks" / "spawn_gemini_preregistered.sh"
CHILD_MAIL = ROOT / "bin" / "agentstack-gemini-child-mail"


def test_gemini_launcher_dry_run_does_not_require_agi_binary() -> None:
    """CI must be able to validate the provider without installing Antigravity."""
    with tempfile.TemporaryDirectory() as tmp:
        env = os.environ.copy()
        env["AGENTSTACK_GEMINI_BIN"] = "agy-definitely-not-installed-for-test"
        env["AGENTSTACK_GEMINI_MODEL"] = "gemini-3.8-flash-high"
        env["AGENTSTACK_GEMINI_EFFORT"] = "high"
        result = subprocess.run(
            ["bash", str(LAUNCHER), "--dry-run", tmp],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
    assert result.returncode == 0, result.stderr
    assert "dry-run cwd=" in result.stdout
    assert "agy-definitely-not-installed-for-test" in result.stdout
    assert "--model gemini-3.8-flash-high" in result.stdout
    assert "--effort high" in result.stdout


def test_gemini_runtime_never_auto_approves_permissions() -> None:
    for path in (LAUNCHER, CHILD, PREREG_CHILD):
        text = path.read_text(encoding="utf-8")
        assert "--dangerously-skip-permissions" not in text
        assert "--assume-yes" not in text


def test_gemini_child_cleanup_is_launcher_owned_and_postposed() -> None:
    text = CHILD.read_text(encoding="utf-8")
    report_at = text.index(" report --project-key")
    release_at = text.index(" release --project-key", report_at)
    retire_at = text.index(" retire --project-key", release_at)
    cleanup_at = text.index("cleanup-child-agent.sh", retire_at)
    assert report_at < release_at < retire_at < cleanup_at

    helper = CHILD_MAIL.read_text(encoding="utf-8")
    assert 'subject_state = "complete" if status == "SUCCESS" else "incomplete"' in helper
    assert 'if status == "SUCCESS" and not response:' in helper
    assert 'if args.runner_status not in (None, 0):' in helper


def test_gemini_entrypoints_parse_without_running_agi() -> None:
    scripts = (
        LAUNCHER,
        ROOT / "bin" / "agentstack-gemini-bootstrap",
        ROOT / "bin" / "agentstack-gemini-mcp",
        ROOT / "bin" / "agentstack-gemini-setup",
        CHILD,
        PREREG_CHILD,
    )
    for path in scripts:
        result = subprocess.run(
            ["bash", "-n", str(path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, f"{path.name}: {result.stderr}"
