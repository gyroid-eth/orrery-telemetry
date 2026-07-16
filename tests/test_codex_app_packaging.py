from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-codex-app-integration.sh"
EXPORTER = ROOT / "scripts" / "export-component.sh"


def _environment(home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment["CODEX_HOME"] = str(home / ".codex")
    environment.pop("AGENTSTACK_CODEX_APP_INSTALL_DIR", None)
    environment.pop("AGENTSTACK_CODEX_APP_RUNTIME_DIR", None)
    return environment


def _install_args(home: Path) -> list[str]:
    return [
        str(INSTALLER),
        "--no-service",
        "--project-key",
        str(home / "project"),
        "--agent-mail-url",
        "http://127.0.0.1:8765/api/",
        "--agent-mail-env",
        str(home / ".mcp_agent_mail" / ".env"),
        "--signals-dir",
        str(home / ".mcp_agent_mail" / "signals"),
    ]


def _prepare_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / "project").mkdir()
    mail_env = home / ".mcp_agent_mail" / ".env"
    mail_env.parent.mkdir()
    mail_env.write_text(
        "HTTP_BEARER_" + "TOKEN=example-secret-value\n",
        encoding="utf-8",
    )
    return home


def _plugin_list(home: Path) -> dict:
    result = subprocess.run(
        ["codex", "plugin", "list", "--json"],
        env=_environment(home),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_installer_dry_run_does_not_write_clean_home(tmp_path):
    home = _prepare_home(tmp_path)
    install_dir = home / ".agentstack" / "integrations" / "codex_app"
    result = subprocess.run(
        [*_install_args(home), "--dry-run"],
        env=_environment(home),
        capture_output=True,
        text=True,
        check=True,
    )

    assert "Dry-run complete: no files were written." in result.stdout
    assert not install_dir.exists()
    assert _plugin_list(home)["installed"] == []


def test_clean_home_install_uninstall_reinstall(tmp_path):
    home = _prepare_home(tmp_path)
    environment = _environment(home)
    install_dir = home / ".agentstack" / "integrations" / "codex_app"
    runtime_dir = home / ".agentstack" / "runtime" / "codex-app"

    subprocess.run(_install_args(home), env=environment, check=True)

    env_file = install_dir / "env.sh"
    assert env_file.is_file()
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert "example-secret-value" not in env_file.read_text(encoding="utf-8")
    manifest = json.loads(
        (install_dir / "install-state.json").read_text(encoding="utf-8")
    )
    assert manifest["plugin"]["id"] == "agentstack-codex-app@agentstack-local"
    with (install_dir / "launchd" / "org.agentstack.codex-app-bridge.plist").open(
        "rb"
    ) as handle:
        plist = plistlib.load(handle)
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["ProgramArguments"] == [
        "/bin/bash",
        str(install_dir / "bin" / "run-bridge"),
    ]

    installed = _plugin_list(home)["installed"]
    assert [item["pluginId"] for item in installed] == [
        "agentstack-codex-app@agentstack-local"
    ]
    cached = (
        home
        / ".codex"
        / "plugins"
        / "cache"
        / "agentstack-local"
        / "agentstack-codex-app"
        / "0.1.0"
    )
    assert (cached / "src" / "agentstack_codex_app" / "mcp_server.py").is_file()
    assert (cached / "schemas" / "migrations" / "001_delivery_state.sql").is_file()
    assert (cached / "scripts" / "run-mcp.sh").is_file()
    mcp = subprocess.run(
        [str(cached / "scripts" / "run-mcp.sh")],
        input=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        + "\n",
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(mcp.stdout)["result"]["serverInfo"]["name"] == "agentstack"

    doctor = subprocess.run(
        [
            str(install_dir / "bin" / "doctor-codex-app-integration"),
            "--allow-stopped",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "ok: Codex plugin registered" in doctor.stdout
    assert "example-secret-value" not in doctor.stdout + doctor.stderr
    assert not list((install_dir / "src").rglob("__pycache__"))

    subprocess.run(
        [str(install_dir / "bin" / "uninstall-codex-app-integration")],
        env=environment,
        check=True,
    )
    assert not install_dir.exists()
    assert runtime_dir.is_dir()
    assert _plugin_list(home)["installed"] == []

    subprocess.run(_install_args(home), env=environment, check=True)
    assert install_dir.is_dir()
    assert len(_plugin_list(home)["installed"]) == 1
    subprocess.run(
        [
            str(install_dir / "bin" / "uninstall-codex-app-integration"),
            "--purge-data",
        ],
        env=environment,
        check=True,
    )
    assert not install_dir.exists()
    assert not runtime_dir.exists()
    assert _plugin_list(home)["installed"] == []


def test_export_gate_builds_allowlisted_token_free_artifact(tmp_path):
    home = _prepare_home(tmp_path)
    destination = tmp_path / "exported"
    environment = _environment(home)
    environment["AGENTSTACK_CODEX_APP_INSTALL_DIR"] = str(
        home / "no-live-integration"
    )
    result = subprocess.run(
        [
            str(EXPORTER),
            "codex-app",
            str(destination),
            "--skip-tests",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "Export complete:" in result.stdout
    assert (
        destination
        / "integrations"
        / "codex_app"
        / "src"
        / "agentstack_codex_app"
        / "daemon.py"
    ).is_file()
    exported_env = destination / "integrations" / "codex_app" / "env.sh"
    assert exported_env.stat().st_mode & 0o777 == 0o600
    assert "/workspace/example" in exported_env.read_text(encoding="utf-8")
    assert not list(destination.rglob("*.sqlite3"))
    assert not list(destination.rglob("__pycache__"))
    assert shutil.which("codex") is not None
