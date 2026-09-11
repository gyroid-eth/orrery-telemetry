"""Regression coverage for the upstream-facing optional Gemini installer."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install-gemini-provider.sh"


def _seed_fake_core(root: pathlib.Path) -> None:
    (root / "dashboard").mkdir(parents=True)
    (root / "dashboard" / "server.py").write_text(
        """
def _is_agent_process_name(name, program):
    return program == 'antigravity' and name == 'agy'

def _agent_process_alive(pane_pid, process_tree, program):
    return None
""".lstrip(),
        encoding="utf-8",
    )
    # The current core service runner is the extension point that selects an
    # optional provider_server.py when one has been installed.  Model that
    # contract in the fake core so this fixture represents a current install,
    # rather than an older core that the provider installer intentionally
    # rejects during preflight.
    (root / "dashboard" / "service_runner.py").write_text(
        """
from pathlib import Path

HERE = Path(__file__).resolve().parent

def _default_server_path():
    provider_server = HERE / 'provider_server.py'
    return provider_server if provider_server.is_file() else HERE / 'server.py'
""".lstrip(),
        encoding="utf-8",
    )
    (root / "install-state.json").write_text(
        json.dumps({"owned_files": [], "owned_dirs": []}) + "\n",
        encoding="utf-8",
    )

    required_files = (
        "bin/lib/agentstack-register.sh",
        "hooks/project-context.sh",
    )
    required_executables = (
        "bin/agentstack-preregister-child",
        "hooks/cleanup-child-agent.sh",
        "integrations/codex_app/plugin/scripts/run-mcp.sh",
    )
    for relative in required_files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture\n", encoding="utf-8")
    for relative in required_executables:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)


def test_installer_dry_run_succeeds_without_agi_and_does_not_mutate_core() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        install_root = pathlib.Path(tmp) / "agentstack"
        _seed_fake_core(install_root)
        manifest = install_root / "install-state.json"
        before = manifest.read_text(encoding="utf-8")

        env = os.environ.copy()
        env.update(
            {
                "AGENTSTACK_PYTHON": sys.executable,
                # Deliberately exclude user-local binary locations where agy
                # is normally installed. Core shell utilities remain present.
                "PATH": "/usr/bin:/bin",
            }
        )
        result = subprocess.run(
            [
                "/bin/bash",
                str(INSTALLER),
                "--install-dir",
                str(install_root),
                "--dry-run",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert "agy is not installed/on PATH" in result.stdout
        assert "Gemini provider payload dry-run complete" in result.stdout
        assert manifest.read_text(encoding="utf-8") == before
        assert not (install_root / "bin" / "agent-start-gemini").exists()


def test_optional_installer_never_replaces_core_dashboard() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    files_block = text.split("FILES=(", 1)[1].split(")", 1)[0]
    assert "dashboard/server.py" not in files_block
    assert "dashboard/service_runner.py" not in files_block
    assert "dashboard/provider_runtime.py" not in files_block
    assert "dashboard/provider_classification.py" not in files_block


def test_optional_installer_is_executable() -> None:
    assert INSTALLER.stat().st_mode & 0o100
