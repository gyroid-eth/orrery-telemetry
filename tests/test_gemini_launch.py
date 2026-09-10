#!/usr/bin/env python3
"""Regression coverage for the Google Antigravity CLI integration."""
from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_LAUNCHER = _ROOT / "bin" / "agent-start-gemini"
_BOOTSTRAP = _ROOT / "bin" / "agentstack-gemini-bootstrap"
_SETUP = _ROOT / "bin" / "agentstack-gemini-setup"
_MCP = _ROOT / "bin" / "agentstack-gemini-mcp"
_CHILD = _ROOT / "hooks" / "spawn_gemini_child.sh"
_PREREG_CHILD = _ROOT / "hooks" / "spawn_gemini_preregistered.sh"
_CHILD_MAIL = _ROOT / "bin" / "agentstack-gemini-child-mail"
_STREAM = _ROOT / "bin" / "agentstack-gemini-stream"


def _run_setup(
    config: pathlib.Path,
    *args: str,
    bearer_mode: str = "disabled",
    token: str = "",
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "AGENTSTACK_HOME": str(config.parent / "agentstack-home"),
            "AGENTSTACK_LABEL_PREFIX": "org.agentstack.test.gemini",
            "AGENTSTACK_GEMINI_MCP_CONFIG": str(config),
            "AGENTSTACK_MCP_URL": "http://127.0.0.1:18765/mcp",
            "AGENTSTACK_MAIL_HTTP_BEARER_MODE": bearer_mode,
            "MCP_AGENT_MAIL_TOKEN": token,
        }
    )
    return subprocess.run(
        ["bash", str(_SETUP), *args],
        cwd=_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_gemini_shell_files_parse_with_bash() -> None:
    for path in (_LAUNCHER, _BOOTSTRAP, _SETUP, _MCP, _CHILD, _PREREG_CHILD):
        result = subprocess.run(
            ["bash", "-n", str(path)],
            cwd=_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


def test_gemini_python_helpers_compile() -> None:
    for path in (_CHILD_MAIL, _STREAM):
        result = subprocess.run(
            [os.environ.get("PYTHON", "python3"), "-m", "py_compile", str(path)],
            cwd=_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


def test_gemini_entrypoints_are_executable() -> None:
    for path in (
        _LAUNCHER,
        _BOOTSTRAP,
        _SETUP,
        _MCP,
        _CHILD,
        _PREREG_CHILD,
        _CHILD_MAIL,
        _STREAM,
    ):
        assert path.stat().st_mode & stat.S_IXUSR, path


def test_launcher_uses_current_antigravity_cli_contract() -> None:
    text = _LAUNCHER.read_text(encoding="utf-8")
    assert 'AGENTSTACK_GEMINI_BIN:-agy' in text
    assert 'AGENTSTACK_GEMINI_MODEL:-gemini-3.8-flash-high' in text
    assert 'AGENTSTACK_GEMINI_EFFORT:-high' in text
    assert '--model $(printf \'%q\' "$GEMINI_MODEL")' in text
    assert '--effort $(printf \'%q\' "$GEMINI_EFFORT")' in text
    assert "GEMINI_API_KEY" not in text
    assert "--dangerously-skip-permissions" not in text


def test_top_level_launcher_preserves_shell_after_interactive_exit() -> None:
    text = _LAUNCHER.read_text(encoding="utf-8")
    in_tmux = text.split(
        "# Already inside a real tmux pane:", 1
    )[1].split("\nfi\n\nGEMINI_CMD=", 1)[0]
    assert 'GEMINI_CMD="$(build_gemini_cmd)"' in in_tmux
    assert 'eval "$GEMINI_CMD"' in in_tmux
    assert 'eval "exec ' not in in_tmux

    # The launcher-created top-level tmux path must also leave a login shell
    # after the Antigravity REPL exits. In both paths the observable lifecycle
    # is therefore live agy -> shell husk (`finished`), not an implicit child-
    # style cleanup/session teardown.
    assert '; exec \\"\\$SHELL\\" -l"' in text


def test_bootstrap_registers_antigravity_runtime_and_model() -> None:
    text = _BOOTSTRAP.read_text(encoding="utf-8")
    assert 'ags_register_session "$PROJECT_KEY" "antigravity" "$GEMINI_MODEL" "agy"' in text
    assert 'AGENTSTACK_GEMINI_MODEL:-gemini-3.8-flash-high' in text


def test_delegated_child_uses_worktree_stream_input_and_preregistered_identity() -> None:
    text = _CHILD.read_text(encoding="utf-8")
    assert '"$PREREGISTER" --project-key "$PROJECT_KEY" --program antigravity' in text
    assert 'git -C "$SOURCE_REPO" worktree add -b "$BRANCH_NAME" "$WORKTREE_DIR" "$BASE_REV"' in text
    assert '--input-format stream-json --output-format stream-json' in text
    assert 'cat $(printf \'%q\' "$TASK_EVENT_FILE")' in text
    assert "--dangerously-skip-permissions" not in text
    assert 'MCP_WRAPPER="$AGENTSTACK_HOME_DIR/bin/agentstack-gemini-mcp"' in text
    assert '"command": os.environ["AGS_GEMINI_MCP_COMMAND"]' in text
    assert '"orrery-mail"' in text
    assert "AGENTSTACK_PROXY_TOKEN_FILE" not in text
    assert "AGS_GEMINI_TOKEN_FILE" not in text
    assert "AGS_GEMINI_PROXY_RUNNER" not in text


def test_both_child_routes_hide_owner_token_path_from_workspace_mcp_config() -> None:
    for path in (_CHILD, _PREREG_CHILD):
        text = path.read_text(encoding="utf-8")
        assert 'MCP_WRAPPER="$AGENTSTACK_HOME_DIR/bin/agentstack-gemini-mcp"' in text
        assert 'AGS_GEMINI_MCP_COMMAND="$MCP_WRAPPER"' in text
        assert '"command": os.environ["AGS_GEMINI_MCP_COMMAND"]' in text
        assert "AGENTSTACK_PROXY_TOKEN_FILE" not in text
        assert "AGS_GEMINI_TOKEN_FILE" not in text


def test_delegated_child_lifecycle_is_launcher_owned() -> None:
    text = _CHILD.read_text(encoding="utf-8")
    assert 'mail_helper reserve --project-key "$PROJECT_KEY"' in text
    assert '$(printf \'%q\' "$MAIL_HELPER") report --project-key' in text
    assert '$(printf \'%q\' "$MAIL_HELPER") release --project-key' in text
    assert '$(printf \'%q\' "$MAIL_HELPER") retire --project-key' in text
    assert 'rm -f $(printf \'%q\' "$TASK_EVENT_FILE") $(printf \'%q\' "$TOKEN_FILE")' in text
    helper = _CHILD_MAIL.read_text(encoding="utf-8")
    assert 'registration_token=token' in helper
    assert 'subject_state = "complete" if status == "SUCCESS" else "incomplete"' in helper
    assert 'subject=f"Gemini child {subject_state}: {args.agent_name}"' in helper
    assert 'importance="normal" if status == "SUCCESS" else "high"' in helper
    assert 'path.stat().st_mode & 0o777' in helper
    assert 'if mode & 0o077:' in helper


def test_workspace_mcp_config_is_ignored() -> None:
    ignore = (_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".agents/mcp_config.json" in ignore


def _expected_session_bound_entry(config: pathlib.Path) -> dict:
    return {
        "command": str(config.parent / "agentstack-home" / "bin" / "agentstack-gemini-mcp"),
        "args": [],
    }


def test_mcp_setup_preserves_unrelated_servers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config = pathlib.Path(tmp) / "mcp_config.json"
        config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "keep-me": {
                            "serverUrl": "https://example.invalid/mcp"
                        }
                    },
                    "other": {"preserved": True},
                }
            ),
            encoding="utf-8",
        )
        result = _run_setup(config, "--print")
        assert result.returncode == 0, result.stderr
        rendered = json.loads(result.stdout)
        assert rendered["other"] == {"preserved": True}
        assert rendered["mcpServers"]["keep-me"] == {
            "serverUrl": "https://example.invalid/mcp"
        }
        assert rendered["mcpServers"]["orrery-mail"] == _expected_session_bound_entry(config)


def test_mcp_setup_never_persists_mail_bearer_or_owner_token() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config = pathlib.Path(tmp) / "mcp_config.json"
        result = _run_setup(
            config,
            "--print",
            bearer_mode="enabled",
            token="secret-for-test",
        )
        assert result.returncode == 0, result.stderr
        entry = json.loads(result.stdout)["mcpServers"]["orrery-mail"]
        assert entry == _expected_session_bound_entry(config)
        assert "secret-for-test" not in result.stdout
        assert "Authorization" not in result.stdout


def test_mcp_setup_uninstall_removes_only_orrery_entry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config = pathlib.Path(tmp) / "mcp_config.json"
        config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "orrery-mail": {"command": "/old/runner", "args": []},
                        "keep-me": {"serverUrl": "https://example.invalid/mcp"},
                    }
                }
            ),
            encoding="utf-8",
        )
        result = _run_setup(config, "--uninstall")
        assert result.returncode == 0, result.stderr
        rendered = json.loads(config.read_text(encoding="utf-8"))
        assert "orrery-mail" not in rendered["mcpServers"]
        assert rendered["mcpServers"]["keep-me"] == {
            "serverUrl": "https://example.invalid/mcp"
        }


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__]))
