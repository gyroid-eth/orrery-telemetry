#!/usr/bin/env python3
"""Regression coverage for delegated Antigravity headless execution."""
from __future__ import annotations

import json
import os
import pathlib
import runpy
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHILD = ROOT / "hooks" / "spawn_gemini_child.sh"
PREREGISTERED = ROOT / "hooks" / "spawn_gemini_preregistered.sh"
STREAM = ROOT / "bin" / "agentstack-gemini-stream"
CHILD_MAIL = ROOT / "bin" / "agentstack-gemini-child-mail"


def _run_stream(events: list[dict]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        result_log = pathlib.Path(tmp) / "result.ndjson"
        payload = "".join(json.dumps(event) + "\n" for event in events)
        return subprocess.run(
            [sys.executable, str(STREAM), str(result_log)],
            cwd=ROOT,
            input=payload,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )


def test_stream_accepts_success_with_textual_response() -> None:
    result = _run_stream(
        [
            {
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": "Task completed without changes.",
                    "denied_actions": [],
                },
            }
        ]
    )
    assert result.returncode == 0, result.stderr
    assert "status=SUCCESS" in result.stdout


def test_stream_rejects_success_without_textual_response() -> None:
    result = _run_stream(
        [
            {
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": "",
                    "denied_actions": [],
                },
            }
        ]
    )
    assert result.returncode != 0
    assert "no textual response" in result.stderr


def test_stream_rejects_success_when_required_action_was_denied() -> None:
    result = _run_stream(
        [
            {
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": "I could not inspect the requested file.",
                    "denied_actions": [
                        {"action": "read_file", "display_name": "ReadFile"}
                    ],
                },
            }
        ]
    )
    assert result.returncode != 0
    assert "required actions were denied" in result.stderr
    assert "read_file" in result.stderr


def test_stream_rejects_non_success_result() -> None:
    result = _run_stream(
        [
            {
                "event": "result",
                "result": {"status": "ERROR", "response": "failed"},
            }
        ]
    )
    assert result.returncode != 0
    assert "status=ERROR" in result.stderr


def test_child_prompts_anchor_resources_to_the_worktree() -> None:
    for path in (CHILD, PREREGISTERED):
        text = path.read_text(encoding="utf-8")
        assert '"$WORKTREE_DIR" "$SOURCE_REPO"' in text
        assert "Declared resources (worktree-anchored):" in text
        assert "Do not search the source checkout, $HOME, or parent directories" in text
        assert "If a declared resource is absent from this worktree" in text


def test_child_runners_propagate_stream_validation_failure_to_parent_report() -> None:
    for path in (CHILD, PREREGISTERED):
        text = path.read_text(encoding="utf-8")
        assert 'pipeline_status=("\\${PIPESTATUS[@]}")' in text
        assert 'stream_status="\\${pipeline_status[2]:-1}"' in text
        assert 'child_status="\\$agy_status"' in text
        assert 'child_status="\\$stream_status"' in text
        assert '--runner-status "\\$child_status"' in text
        assert 'exit "\\$child_status"' in text


def test_child_launchers_keep_mcp_config_out_of_git_without_shared_exclude_mutation() -> None:
    for path in (CHILD, PREREGISTERED):
        text = path.read_text(encoding="utf-8")
        assert "GIT_EXCLUDES_FILE" in text
        assert "core.excludesFile" in text
        assert "GIT_CONFIG_COUNT" in text
        assert "git_excludes_file=$(printf '%q'" in text
        assert 'GIT_CONFIG_VALUE_\\${git_config_count}=\\$git_excludes_file' in text
        assert ".agents/mcp_config.json" in text
        assert "rev-parse --git-path info/exclude" not in text
        assert "EXCLUDE_ADDED" not in text
        assert "remove_transient_exclude" not in text


def test_process_local_git_excludes_hide_mcp_config_without_mutating_repo(tmp_path) -> None:
    repo = tmp_path / "repo with spaces"
    repo.mkdir()
    init = subprocess.run(
        ["git", "init", str(repo)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert init.returncode == 0, init.stderr

    mcp = repo / ".agents" / "mcp_config.json"
    mcp.parent.mkdir()
    mcp.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    (repo / "visible.txt").write_text("visible\n", encoding="utf-8")

    shared_exclude = repo / ".git" / "info" / "exclude"
    shared_before = shared_exclude.read_bytes()
    local_exclude = tmp_path / "runtime with spaces" / "gemini excludes"
    local_exclude.parent.mkdir()
    local_exclude.write_text(".agents/mcp_config.json\n", encoding="utf-8")

    env = dict(os.environ)
    for key in list(env):
        if key == "GIT_CONFIG_COUNT" or key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
            env.pop(key, None)
    env.update(
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="core.excludesFile",
        GIT_CONFIG_VALUE_0=str(local_exclude),
    )
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--short", "--untracked-files=all"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert status.returncode == 0, status.stderr
    assert "visible.txt" in status.stdout
    assert ".agents/mcp_config.json" not in status.stdout
    assert shared_exclude.read_bytes() == shared_before


def test_child_mail_rejects_resource_paths_that_escape_the_project() -> None:
    namespace = runpy.run_path(str(CHILD_MAIL))
    parse_paths = namespace["_paths"]

    assert parse_paths("src/**,tests/**") == ["src/**", "tests/**"]
    for raw in ("../secret", "src/../../secret", "/tmp/secret", "~/.ssh"):
        try:
            parse_paths(raw)
        except SystemExit as exc:
            assert "project-relative" in str(exc)
        else:
            raise AssertionError(f"unsafe resource path was accepted: {raw!r}")


def test_child_mail_marks_incomplete_results_for_parent() -> None:
    text = CHILD_MAIL.read_text(encoding="utf-8")
    assert 'if status == "SUCCESS" and not response:' in text
    assert 'if denied:' in text
    assert "Antigravity denied required actions" in text
    assert "args.runner_status not in (None, 0)" in text
    assert 'status = "INCOMPLETE"' in text
    assert 'subject_state = "complete" if status == "SUCCESS" else "incomplete"' in text


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__]))
