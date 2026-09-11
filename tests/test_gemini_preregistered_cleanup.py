"""Failure cleanup contract for the dashboard Gemini adapter."""
from __future__ import annotations

import pathlib
import subprocess


ROOT = pathlib.Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "hooks" / "spawn_gemini_preregistered.sh"


def test_preregistered_launcher_parses_with_bash() -> None:
    result = subprocess.run(
        ["bash", "-n", str(LAUNCHER)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_preregistered_failure_cleanup_retires_identity_before_deleting_token() -> None:
    text = LAUNCHER.read_text(encoding="utf-8")
    cleanup = text[text.index("cleanup_failure() {"):text.index("trap cleanup_failure EXIT")]

    release = 'mail_helper release --project-key "$PROJECT_KEY"'
    retire = 'mail_helper retire --project-key "$PROJECT_KEY"'
    token_delete = 'rm -f "$TASK_EVENT_FILE" "$RUNNER_FILE" "$MCP_CONFIG" "$DURABLE_TOKEN"'

    assert release in cleanup
    assert retire in cleanup
    assert token_delete in cleanup
    assert cleanup.index(release) < cleanup.index(retire) < cleanup.index(token_delete)
