"""Regression coverage for Claude Code's fixed-name agent-mail registration."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parent.parent
MERGER = ROOT / "scripts" / "lib" / "merge_claude_mcp.py"
DOCTOR = ROOT / "scripts" / "doctor.sh"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MERGER), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def test_merge_previews_redacted_token_and_preserves_other_servers(tmp_path):
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({
        "mcpServers": {"user-server": {"command": "user-command"}},
        "projects": {"/project": {"mcpServers": {"project-server": {}}}},
    }))
    mail_env = tmp_path / "mail.env"
    mail_env.write_text("HTTP_BEARER_TOKEN=super-secret-bearer\n")
    result = tmp_path / "result.json"
    common = (
        "--config", str(config),
        "--mcp-url", "http://127.0.0.1:18765/mcp",
        "--mail-env", str(mail_env),
        "--backup-dir", str(tmp_path / "backups"),
    )

    preview = _run(*common, "--dry-run")
    assert preview.returncode == 0, preview.stderr
    assert "<redacted>" in preview.stdout
    assert "super-secret-bearer" not in preview.stdout + preview.stderr
    assert "mcp-agent-mail" not in json.loads(config.read_text())["mcpServers"]

    applied = _run(*common, "--result-json", str(result))
    assert applied.returncode == 0, applied.stderr
    assert "super-secret-bearer" not in applied.stdout + applied.stderr
    installed = json.loads(config.read_text())
    assert installed["mcpServers"]["user-server"] == {"command": "user-command"}
    assert installed["projects"]["/project"]["mcpServers"] == {
        "project-server": {}
    }
    assert installed["mcpServers"]["mcp-agent-mail"] == {
        "type": "http",
        "url": "http://127.0.0.1:18765/mcp",
        "headers": {"Authorization": "Bearer super-secret-bearer"},
    }
    assert os.stat(config).st_mode & 0o777 == 0o600
    recorded = json.loads(result.read_text())
    assert recorded["server_name"] == "mcp-agent-mail"
    assert "super-secret-bearer" not in result.read_text()
    assert pathlib.Path(recorded["backup"]["backup_path"]).is_file()


def test_remove_restores_previous_fixed_entry_without_touching_others(tmp_path):
    config = tmp_path / ".claude.json"
    previous = {"command": "user-owned-agent-mail", "args": ["--keep"]}
    config.write_text(json.dumps({
        "mcpServers": {
            "mcp-agent-mail": previous,
            "other": {"command": "other"},
        }
    }))
    result = tmp_path / "result.json"
    common = (
        "--config", str(config),
        "--mcp-url", "http://127.0.0.1:8765/mcp",
        "--backup-dir", str(tmp_path / "backups"),
    )
    assert _run(*common, "--result-json", str(result)).returncode == 0
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "claude_mcp_merge": json.loads(result.read_text())
    }))

    removed = _run(
        "--remove",
        "--config", str(config),
        "--backup-dir", str(tmp_path / "backups"),
        "--manifest", str(manifest),
    )
    assert removed.returncode == 0, removed.stderr
    restored = json.loads(config.read_text())["mcpServers"]
    assert restored == {
        "mcp-agent-mail": previous,
        "other": {"command": "other"},
    }


def test_remove_keeps_a_user_modified_entry(tmp_path):
    config = tmp_path / ".claude.json"
    config.write_text("{}\n")
    result = tmp_path / "result.json"
    common = (
        "--config", str(config),
        "--mcp-url", "http://127.0.0.1:8765/mcp",
        "--backup-dir", str(tmp_path / "backups"),
    )
    assert _run(*common, "--result-json", str(result)).returncode == 0
    changed = json.loads(config.read_text())
    changed["mcpServers"]["mcp-agent-mail"]["url"] = "http://user.example/mcp"
    config.write_text(json.dumps(changed))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "claude_mcp_merge": json.loads(result.read_text())
    }))

    removed = _run(
        "--remove",
        "--config", str(config),
        "--backup-dir", str(tmp_path / "backups"),
        "--manifest", str(manifest),
    )
    assert removed.returncode == 0, removed.stderr
    assert "Kept modified" in removed.stdout
    assert json.loads(config.read_text())["mcpServers"]["mcp-agent-mail"][
        "url"
    ] == "http://user.example/mcp"


def test_upgrade_keeps_the_pre_agentstack_entry_as_uninstall_baseline(tmp_path):
    config = tmp_path / ".claude.json"
    previous = {"command": "user-owned-agent-mail"}
    config.write_text(json.dumps({
        "mcpServers": {"mcp-agent-mail": previous}
    }))
    result = tmp_path / "result.json"
    base = (
        "--config", str(config),
        "--backup-dir", str(tmp_path / "backups"),
        "--result-json", str(result),
    )
    assert _run(
        *base, "--mcp-url", "http://127.0.0.1:8765/mcp"
    ).returncode == 0
    assert _run(
        *base,
        "--mcp-url", "http://127.0.0.1:28765/mcp",
        "--existing-result", str(result),
    ).returncode == 0
    recorded = json.loads(result.read_text())
    assert recorded["previous_entry_existed"] is True
    assert "operation_backup" in recorded
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"claude_mcp_merge": recorded}))

    removed = _run(
        "--remove",
        "--config", str(config),
        "--backup-dir", str(tmp_path / "backups"),
        "--manifest", str(manifest),
    )
    assert removed.returncode == 0, removed.stderr
    assert json.loads(config.read_text())["mcpServers"]["mcp-agent-mail"] == previous


def test_invalid_mcp_servers_is_fail_closed(tmp_path):
    config = tmp_path / ".claude.json"
    original = '{"mcpServers": ["not-an-object"]}\n'
    config.write_text(original)

    result = _run(
        "--config", str(config),
        "--mcp-url", "http://127.0.0.1:8765/mcp",
        "--backup-dir", str(tmp_path / "backups"),
    )

    assert result.returncode == 1
    assert "mcpServers must be an object" in result.stderr
    assert config.read_text() == original


def test_doctor_warns_and_prints_safe_registration_commands(tmp_path):
    home = tmp_path / "home"
    install = home / ".agentstack"
    project = tmp_path / "project"
    project.mkdir()
    runtime = install / "runtime"
    runtime.mkdir(parents=True)
    mail_db = tmp_path / "mail.sqlite3"
    mail_db.touch()
    claude_json = home / ".claude.json"
    env_file = install / "env.sh"
    env_file.write_text("\n".join((
        f"export AGENTSTACK_MAIL_DB='{mail_db}'",
        "export AGENTSTACK_MCP_URL='http://127.0.0.1:18765/mcp'",
        f"export AGENTSTACK_CLAUDE_JSON='{claude_json}'",
        f"export AGENTSTACK_PROJECT_KEY='{project}'",
        f"export AGENTSTACK_RUNTIME_DIR='{runtime}'",
        f"export AGENTSTACK_DASHBOARD_LOG='{runtime / 'dashboard.log'}'",
        "",
    )))
    (runtime / "dashboard.log").touch()
    (install / "install-state.json").write_text('{"services": []}\n')
    hooks = install / "hooks"
    hooks.mkdir()
    (hooks / "spawn_child.sh").write_text("#!/bin/sh\n")
    (hooks / "spawn_child.sh").chmod(0o755)
    dashboard = install / "dashboard"
    dashboard.mkdir()
    (dashboard / "server.py").touch()
    (dashboard / "service_runner.py").touch()

    result = subprocess.run(
        ["bash", str(DOCTOR), "--install-dir", str(install)],
        env={**os.environ, "HOME": str(home), "AGENTSTACK_PYTHON": sys.executable},
        text=True,
        capture_output=True,
        check=False,
    )

    assert "Claude MCP mcp-agent-mail is not registered" in result.stderr
    assert "/delegate cannot use agent-mail" in result.stderr
    assert "agentstack-merge-claude-mcp" in result.stderr
    assert "--dry-run" in result.stderr
