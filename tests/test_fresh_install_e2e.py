"""Opt-in E2E for the boundary that matters most: a truly fresh machine."""

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
import pathlib
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent
UPSTREAM_AGENT_MAIL = "https://github.com/Dicklesworthstone/mcp_agent_mail.git"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _require_e2e_prerequisites() -> None:
    if os.environ.get("AGENTSTACK_E2E") != "1":
        pytest.skip("set AGENTSTACK_E2E=1 to run the networked fresh-install E2E")

    missing = [name for name in ("git", "uv", "tmux") if not shutil.which(name)]
    if not missing:
        return
    message = "fresh-install E2E prerequisites missing: " + ", ".join(missing)
    if os.environ.get("AGENTSTACK_E2E_CI") == "1":
        pytest.fail(message)
    pytest.skip(message)


def _fresh_env(home: pathlib.Path, project: pathlib.Path) -> dict[str, str]:
    mail_port = _free_port()
    dashboard_port = _free_port()
    while dashboard_port == mail_port:
        dashboard_port = _free_port()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AGENTSTACK_") and key != "PROJECT_KEY"
    }
    env.update(
        {
            "HOME": str(home),
            "AGENTSTACK_PYTHON": sys.executable,
            "AGENTSTACK_HOME": str(home / ".agentstack"),
            "AGENTSTACK_MAIL_DIR": str(home / "mcp_agent_mail"),
            "AGENTSTACK_MAIL_HOME": str(home / ".mcp_agent_mail"),
            "AGENTSTACK_MCP_URL": f"http://127.0.0.1:{mail_port}/mcp",
            "AGENTSTACK_PORT": str(dashboard_port),
            "AGENTSTACK_PROJECT_KEY": str(project),
            "AGENTSTACK_LABEL_PREFIX": f"org.agentstack.e2e.{os.getpid()}",
            "AGENTSTACK_TERMINAL": "none",
            "AGENTSTACK_PATH": os.environ.get("PATH", ""),
        }
    )
    return env


def _stop_pidfile(path: pathlib.Path) -> None:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return
    if pid <= 1:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)


def _load_installed_selftest(path: pathlib.Path):
    loader = SourceFileLoader("installed_agentstack_selftest", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verify_normal_parent_child_messaging(
    install_dir: pathlib.Path, project: pathlib.Path, env: dict[str, str]
) -> None:
    """Exercise product registration helpers without selftest's handshake fallback."""
    env_file = install_dir / "env.sh"
    register_lib = install_dir / "bin" / "lib" / "agentstack-register.sh"
    registered_parent = subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail; . "$1"; . "$2"; ags_mail_load_token; '
            'ags_register_session "$AGENTSTACK_PROJECT_KEY" codex e2e e2e '
            '"$3" "" candidate',
            "product-parent-registration",
            str(env_file),
            str(register_lib),
            str(project),
        ],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert registered_parent.returncode == 0, registered_parent.stderr
    parent = registered_parent.stdout.strip().splitlines()[-1]
    parent_token_file = install_dir / "runtime" / f"agent_token_{parent}"
    parent_token = parent_token_file.read_text(encoding="utf-8").strip()

    child_token_file = install_dir / "runtime" / "e2e-child.token"
    registered_child = subprocess.run(
        [
            str(install_dir / "bin" / "agentstack-preregister-child"),
            "--project-key",
            str(project),
            "--program",
            "codex",
            "--model",
            "e2e",
            "--task-description",
            "fresh-install product messaging E2E",
            "--token-file-out",
            str(child_token_file),
        ],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert registered_child.returncode == 0, registered_child.stderr
    child = registered_child.stdout.strip().splitlines()[-1]
    child_token = child_token_file.read_text(encoding="utf-8").strip()

    module = _load_installed_selftest(install_dir / "bin" / "agentstack-selftest")
    installed_env = module.load_env(install_dir)
    mail = module.AgentMail(
        installed_env["AGENTSTACK_MCP_URL"], module.read_token(installed_env)
    )
    mail.remember_token(parent, parent_token)
    mail.remember_token(child, child_token)

    # Stock agent-mail defaults new identities to a contact-gated policy. The
    # normal launch helpers must deliberately open both ends before the first
    # task message; otherwise this flow is issue 11 even if selftest can repair
    # it by calling request_contact/respond_contact. Stock whois omits this
    # field, so inspect its real SQLite source of truth.
    with sqlite3.connect(installed_env["AGENTSTACK_MAIL_DB"]) as connection:
        policies = dict(
            connection.execute(
                "SELECT a.name, a.contact_policy FROM agents a "
                "JOIN projects p ON p.id = a.project_id "
                "WHERE p.human_key = ? AND a.name IN (?, ?)",
                (str(project), parent, child),
            )
        )
    assert policies == {parent: "open", child: "open"}

    mail.call(
        "send_message",
        {
            "project_key": str(project),
            "sender_name": parent,
            "to": [child],
            "subject": "product flow task",
            "body_md": "parent to child without a selftest contact handshake",
        },
        agent=parent,
    )
    child_inbox = mail.call(
        "fetch_inbox",
        {
            "project_key": str(project),
            "agent_name": child,
            "limit": 10,
        },
        agent=child,
    )
    child_rows = child_inbox.get("result", child_inbox)
    assert any(row.get("subject") == "product flow task" for row in child_rows)

    mail.call(
        "send_message",
        {
            "project_key": str(project),
            "sender_name": child,
            "to": [parent],
            "subject": "product flow reply",
            "body_md": "child to parent without a selftest contact handshake",
        },
        agent=child,
    )
    parent_inbox = mail.call(
        "fetch_inbox",
        {
            "project_key": str(project),
            "agent_name": parent,
            "limit": 10,
        },
        agent=parent,
    )
    parent_rows = parent_inbox.get("result", parent_inbox)
    assert any(row.get("subject") == "product flow reply" for row in parent_rows)


def test_real_fresh_install_reaches_selftest_exit_zero(tmp_path):
    _require_e2e_prerequisites()
    home = tmp_path / "empty-home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    env = _fresh_env(home, project)
    install_dir = pathlib.Path(env["AGENTSTACK_HOME"])
    mail_dir = pathlib.Path(env["AGENTSTACK_MAIL_DIR"])
    mail_home = pathlib.Path(env["AGENTSTACK_MAIL_HOME"])
    installer = ROOT / "scripts" / "install.sh"

    try:
        installed = subprocess.run(
            ["bash", str(installer)],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=600,
        )
        assert installed.returncode == 0, (
            "fresh install failed\nSTDOUT:\n"
            + installed.stdout
            + "\nSTDERR:\n"
            + installed.stderr
        )
        assert "Install complete:" in installed.stdout
        assert "agent-mail ready at" in installed.stdout
        assert (mail_dir / ".git").is_dir()
        assert (mail_dir / ".venv").is_dir()
        remote = subprocess.run(
            ["git", "-C", str(mail_dir), "remote", "get-url", "origin"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        assert remote == UPSTREAM_AGENT_MAIL

        manifest = json.loads(
            (install_dir / "install-state.json").read_text(encoding="utf-8")
        )
        assert manifest["env"]["AGENTSTACK_MAIL_DB"]
        assert pathlib.Path(manifest["env"]["AGENTSTACK_MAIL_DB"]).is_file()

        selftest = install_dir / "bin" / "agentstack-selftest"
        assert selftest.is_file()
        assert os.access(selftest, os.X_OK)
        _verify_normal_parent_child_messaging(install_dir, project, env)
        verified = subprocess.run(
            [str(selftest), "--install-dir", str(install_dir)],
            cwd=project,
            env=env,
            text=True,
            capture_output=True,
            timeout=180,
        )
        assert verified.returncode == 0, (
            "installed self-test failed\nSTDOUT:\n"
            + verified.stdout
            + "\nSTDERR:\n"
            + verified.stderr
        )
        assert "self-test passed:" in verified.stdout
    finally:
        uninstaller = install_dir / "bin" / "agentstack-uninstall"
        if uninstaller.is_file():
            subprocess.run(
                [str(uninstaller), "--purge-data"],
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                text=True,
                capture_output=True,
                timeout=90,
                check=False,
            )
        # A failed install may not have written a manifest for the uninstaller.
        # Stop only the two exact supervisors created under this test's HOME.
        _stop_pidfile(install_dir / "runtime" / "dashboard.pid")
        _stop_pidfile(mail_home / "agent-mail.pid")
