"""Regression coverage for install-root runtime defaults and launcher guards."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess


ROOT = pathlib.Path(__file__).resolve().parent.parent


def _clean_env(home: pathlib.Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("AGENTSTACK_RUNTIME_DIR", None)
    env.pop("AGENTSTACK_MANAGED_AGENTS_FILE", None)
    env.pop("AGENTSTACK_MAIL_ENV", None)
    env.pop("AGENTSTACK_SIGNALS_DIR", None)
    return env


def test_runtime_fallbacks_live_under_install_root(tmp_path):
    env = _clean_env(tmp_path)
    register = ROOT / "bin" / "lib" / "agentstack-register.sh"
    result = subprocess.run(
        ["bash", "-c", f'source "{register}"; ags_registration_runtime_dir'],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == str(tmp_path / ".agentstack" / "runtime")

    result = subprocess.run(
        [
            "python3",
            "-c",
            "from dashboard.server import RUNTIME_DIR; print(RUNTIME_DIR)",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == str(tmp_path / ".agentstack" / "runtime")


def test_session_index_writer_uses_install_root_runtime(tmp_path):
    env = _clean_env(tmp_path)
    payload = {
        "session_id": "session-1",
        "tool_response": {"id": 42, "name": "WiseFaraday"},
    }
    subprocess.run(
        ["python3", str(ROOT / "hooks" / "record-session-index.py")],
        env=env,
        input=json.dumps(payload),
        text=True,
        check=True,
    )
    record = tmp_path / ".agentstack" / "runtime" / "session_index" / "42.json"
    assert json.loads(record.read_text(encoding="utf-8"))["agent_name"] == "WiseFaraday"
    assert not (tmp_path / ".claude" / "runtime").exists()


def test_runtime_code_has_no_legacy_claude_fallback():
    roots = ["hooks", "bin", "scripts", "dashboard", "integrations", "claude", "codex"]
    offenders = []
    paths = [ROOT / ".env.example"]
    for root in roots:
        paths.extend((ROOT / root).rglob("*"))
    for path in paths:
        if path.is_file() and path.suffix not in {".png", ".pyc"}:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if ".claude/runtime" in text or ".claude/managed_agents.txt" in text:
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_existing_tmux_launch_paths_export_claudecode_guard():
    for relative in ("bin/agent-start", "bin/agent-start-codex"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        branch = text[text.index('if [[ -n "${TMUX:-}" ]]'):]
        branch = branch[:branch.index("\nfi\n")]
        assert "export CLAUDECODE=1" in branch, relative


def test_tcc_guard_accepts_documented_colon_separated_paths(tmp_path):
    env = _clean_env(tmp_path)
    protected = tmp_path / "Folder With Spaces"
    env["AGENTSTACK_TCC_DIRS"] = f"{tmp_path / 'Desktop'}:{protected}"
    register = ROOT / "bin" / "lib" / "agentstack-register.sh"
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{register}"; ags_tcc_dir_is_protected "$1"',
            "tcc-test",
            str(protected / "project"),
        ],
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_tcc_guard_keeps_legacy_whitespace_list_compatibility(tmp_path):
    env = _clean_env(tmp_path)
    protected = tmp_path / "Documents"
    env["AGENTSTACK_TCC_DIRS"] = f"{tmp_path / 'Desktop'} {protected}"
    register = ROOT / "bin" / "lib" / "agentstack-register.sh"
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{register}"; ags_tcc_dir_is_protected "$1"',
            "tcc-test",
            str(protected / "project"),
        ],
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_install_tier_options_are_mutually_exclusive(tmp_path):
    env = _clean_env(tmp_path)
    installer = ROOT / "scripts" / "install.sh"
    for args in (
        ["--dashboard-only", "--scoped"],
        ["--scoped", "--dashboard-only"],
    ):
        result = subprocess.run(
            ["bash", str(installer), *args, "--dry-run"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 2
        assert "mutually exclusive" in result.stderr
        assert not (tmp_path / ".agentstack").exists()


def test_codex_app_installer_uses_clone_env_not_signal_home(tmp_path):
    env = _clean_env(tmp_path)
    installer = ROOT / "scripts" / "install-codex-app-integration.sh"
    result = subprocess.run(
        [
            "bash",
            str(installer),
            "--dry-run",
            "--no-service",
            "--no-plugin",
            "--project-key",
            str(tmp_path / "project"),
            "--agent-mail-url",
            "http://127.0.0.1:8765/api/",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    expected = tmp_path / "mcp_agent_mail" / ".env"
    assert f"bearer reference does not exist yet: {expected}" in result.stderr
    assert str(tmp_path / ".mcp_agent_mail" / ".env") not in result.stderr
    sample = (ROOT / "integrations" / "codex_app" / "env.sh.sample").read_text(
        encoding="utf-8"
    )
    assert 'AGENTSTACK_MAIL_ENV="$HOME/mcp_agent_mail/.env"' in sample
