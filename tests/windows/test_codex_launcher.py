"""Bounded native-Windows regressions for the Codex child launcher."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import tomllib
from types import ModuleType

import psutil
import pytest


ROOT = Path(__file__).resolve().parents[2]
PRIVATE_STATE_PATH = ROOT / "scripts" / "windows" / "private_state.py"
LAUNCHER_PATH = ROOT / "scripts" / "windows" / "codex_launcher.py"
PROXY_PATH = ROOT / "scripts" / "windows" / "run_codex_proxy.py"
OWNED_JOB_PATH = ROOT / "scripts" / "windows" / "owned_job.py"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


private_state = _load_module("private_state", PRIVATE_STATE_PATH)
owned_job = _load_module("owned_job", OWNED_JOB_PATH)
launcher = _load_module("codex_launcher", LAUNCHER_PATH)
proxy = _load_module("run_codex_proxy", PROXY_PATH)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires native Windows process and ACL semantics",
)


def _acl_snapshot(path: Path) -> dict:
    text = private_state.powershell(
        "$a=Get-Acl -LiteralPath $env:ORRERY_ACL_PATH; "
        "$rules=@($a.GetAccessRules($true,$true,"
        "[System.Security.Principal.SecurityIdentifier]) | ForEach-Object { "
        "@{sid=$_.IdentityReference.Value;type=[int]$_.AccessControlType;"
        "rights=[int]$_.FileSystemRights} }); "
        "@{owner=$a.GetOwner("
        "[System.Security.Principal.SecurityIdentifier]).Value;"
        "protected=$a.AreAccessRulesProtected;rules=$rules} | "
        "ConvertTo-Json -Compress -Depth 4",
        ORRERY_ACL_PATH=str(path),
    )
    snapshot = json.loads(text)
    rules = snapshot.get("rules", [])
    if isinstance(rules, dict):
        rules = [rules]
    snapshot["rules"] = rules
    return snapshot


def _assert_current_user_only(path: Path, *, protected: bool | None = True) -> None:
    snapshot = _acl_snapshot(path)
    sid = private_state.current_sid()
    assert snapshot["owner"] == sid
    if protected is not None:
        assert snapshot["protected"] is protected
    assert snapshot["rules"]
    assert {rule["sid"] for rule in snapshot["rules"]} == {sid}
    assert all(rule["type"] == 0 for rule in snapshot["rules"])
    assert any(
        int(rule["rights"]) & 0x1F01FF == 0x1F01FF
        for rule in snapshot["rules"]
    )


def _grant_everyone_read(path: Path) -> None:
    subprocess.run(
        ["icacls", str(path), "/grant", "*S-1-1-0:R"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_private_directory_has_only_current_user_full_control(tmp_path: Path) -> None:
    state = tmp_path / "private-state"
    private_state.create_private_directory(state)

    _assert_current_user_only(state)


def test_unsafe_token_handoff_is_rejected(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    destination_root = private_root / "child-state"
    private_state.create_private_directory(private_root)
    private_state.create_private_directory(destination_root)

    source = tmp_path / "unsafe-handoff.token"
    source.write_text("token-that-must-not-move\n", encoding="utf-8")
    _grant_everyone_read(source)
    snapshot = _acl_snapshot(source)
    assert "S-1-1-0" in {rule["sid"] for rule in snapshot["rules"]}

    destination = destination_root / "owner.token"
    with pytest.raises(PermissionError, match="Private state"):
        private_state.consume_token(source, destination)

    assert source.is_file()
    assert not destination.exists()


def test_private_token_handoff_consumes_source_after_verified_copy(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private"
    destination_root = private_root / "child-state"
    private_state.create_private_directory(private_root)
    private_state.create_private_directory(destination_root)

    source = private_root / "handoff.token"
    source.write_text("one-time-child-token\n", encoding="utf-8")
    private_state.protect_private_file(source)
    destination = destination_root / "owner.token"
    private_state.consume_token(source, destination)

    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "one-time-child-token"
    private_state.require_private(destination)
    _assert_current_user_only(destination, protected=None)


def _wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate(), "condition did not become true before timeout"


def _pid_file_ready(path: Path) -> bool:
    try:
        return path.is_file() and bool(path.read_text(encoding="ascii").strip())
    except (OSError, UnicodeError):
        return False


def _process_alive(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _terminate_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _terminate_pid(pid: int | None) -> None:
    if pid is None:
        return
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except psutil.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def test_stop_owned_terminates_real_child_tree_and_leaves_foreign_process(
    tmp_path: Path,
) -> None:
    owner_script = tmp_path / "owner.py"
    child_pid_file = tmp_path / "child.pid"
    owner_script.write_text(
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        "Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii')\n"
        "child.wait()\n",
        encoding="utf-8",
    )
    owner = subprocess.Popen(
        [sys.executable, str(owner_script), str(child_pid_file)],
        cwd=str(tmp_path),
    )
    foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    child_pid: int | None = None
    try:
        _wait_for(lambda: _pid_file_ready(child_pid_file))
        child_pid = int(child_pid_file.read_text(encoding="ascii"))
        _wait_for(lambda: _process_alive(child_pid))
        parent_record = launcher.process_record(psutil.Process(owner.pid))

        stopped = launcher.stop_owned([parent_record])

        assert owner.poll() is not None
        assert child_pid in stopped
        assert owner.pid in stopped
        assert not _process_alive(child_pid)
        assert foreign.poll() is None
        assert _process_alive(foreign.pid)
    finally:
        if child_pid is None and child_pid_file.is_file():
            try:
                child_pid = int(child_pid_file.read_text(encoding="ascii"))
            except (OSError, ValueError):
                pass
        _terminate_process(owner)
        _terminate_process(foreign)
        _terminate_pid(child_pid)


def test_owned_job_closes_orphaned_grandchild_but_leaves_foreign_process(
    tmp_path: Path,
) -> None:
    child_script = tmp_path / "job-child.py"
    grandchild_pid_file = tmp_path / "grandchild.pid"
    child_script.write_text(
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        "Path(sys.argv[1]).write_text(str(grandchild.pid), encoding='ascii')\n",
        encoding="utf-8",
    )
    foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    job = owned_job.OwnedJob()
    child: subprocess.Popen | None = None
    grandchild_pid: int | None = None
    try:
        child = job.start(
            [sys.executable, str(child_script), str(grandchild_pid_file)],
            cwd=str(tmp_path),
        )
        child.wait(timeout=10)
        _wait_for(lambda: _pid_file_ready(grandchild_pid_file))
        grandchild_pid = int(grandchild_pid_file.read_text(encoding="ascii"))
        _wait_for(lambda: _process_alive(grandchild_pid))
        assert _process_alive(foreign.pid)

        job.close()

        _wait_for(lambda: not _process_alive(grandchild_pid))
        assert foreign.poll() is None
        assert _process_alive(foreign.pid)
    finally:
        job.close()
        _terminate_process(child)
        _terminate_process(foreign)
        _terminate_pid(grandchild_pid)


def test_stale_creation_time_prevents_pid_reuse_termination() -> None:
    foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    try:
        record = launcher.process_record(psutil.Process(foreign.pid))
        record["created"] = float(record["created"]) + 60.0

        assert launcher.matching_process(record) is None
        assert launcher.stop_owned([record]) == []
        assert foreign.poll() is None
        assert _process_alive(foreign.pid)
    finally:
        _terminate_process(foreign)


def test_pane_ready_rejects_blocked_dialogs_even_with_a_prompt_footer() -> None:
    blocked = (
        "Starting MCP\n› Ask Codex to do anything\n  gpt-5.6-sol",
        "Sign in to continue\n› Ask Codex to do anything\n  gpt-5.6-sol",
        "Do you trust this folder?\n❯ Yes\n  gpt-5.6-sol",
        "Setup required\n› Ask Codex to do anything\n  gpt-5.6-sol",
        "Would you like to continue?\n› Ask Codex to do anything\n  gpt-5.6-sol",
        "Approve access\n› Ask Codex to do anything\n  gpt-5.6-sol",
        "Usage limit reached\n› Ask Codex to do anything\n  gpt-5.6-sol",
    )
    for pane in blocked:
        assert launcher.pane_ready(pane) is False, pane


def test_pane_ready_accepts_an_idle_codex_prompt() -> None:
    assert launcher.pane_ready(
        "Codex\n› Ask Codex to do anything\n  gpt-5.6-sol low"
    ) is True


def test_pane_ready_ignores_old_startup_scrollback() -> None:
    assert launcher.pane_ready(
        "Starting MCP\n"
        "old startup details\n"
        "Codex\n› Ask Codex to do anything\n  gpt-5.6-sol low"
    ) is True


def test_proxy_environment_disabled_removes_ambient_bearer_token() -> None:
    environment = {
        "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled",
        "MCP_AGENT_MAIL_TOKEN": "ambient-token-for-this-test",
    }

    result = proxy.proxy_environment(environment)

    assert "MCP_AGENT_MAIL_TOKEN" not in result
    assert environment["MCP_AGENT_MAIL_TOKEN"] == "ambient-token-for-this-test"


def test_proxy_environment_enabled_reads_private_env_without_shell_execution(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private"
    private_state.create_private_directory(private_root)
    mail_env = private_root / "mail.env"
    sentinel = tmp_path / "must-not-be-created"
    mail_env.write_text(
        "HTTP_BEARER_TOKEN='local-test-token'\n"
        f"UNSAFE=$(New-Item -ItemType File -Path '{sentinel}')\n",
        encoding="utf-8",
    )
    private_state.protect_private_file(mail_env)

    result = proxy.proxy_environment(
        {
            "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "enabled",
            "AGENTSTACK_MAIL_ENV": str(mail_env),
        }
    )

    assert result["MCP_AGENT_MAIL_TOKEN"] == "local-test-token"
    assert not sentinel.exists()


def test_proxy_environment_enabled_requires_a_bearer_token(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    private_state.create_private_directory(private_root)
    mail_env = private_root / "mail.env"
    mail_env.write_text("AGENTSTACK_MAIL_HTTP_BEARER_MODE=enabled\n", encoding="utf-8")
    private_state.protect_private_file(mail_env)

    with pytest.raises(ValueError, match="does not contain HTTP_BEARER_TOKEN"):
        proxy.proxy_environment(
            {
                "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "enabled",
                "AGENTSTACK_MAIL_ENV": str(mail_env),
            }
        )


def test_direct_proxy_requires_a_private_owner_token(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    private_state.create_private_directory(private_root)
    token = private_root / "owner.token"
    token.write_text("owner-token", encoding="utf-8")
    private_state.protect_private_file(token)
    environment = {
        "AGENTSTACK_PROXY_AGENT_NAME": "BlueLake",
        "AGENTSTACK_PROXY_TOKEN_FILE": str(token),
    }

    proxy.verify_direct_token(environment)
    _grant_everyone_read(token)
    with pytest.raises(PermissionError, match="Private state"):
        proxy.verify_direct_token(environment)


def test_proxy_config_propagates_child_runtime_environment(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    private_state.create_private_directory(home)
    state = tmp_path / "state"
    python = tmp_path / "python.exe"
    spec = {
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
        "python": str(python),
        "name": "BlueLake",
        "state": str(state),
        "project": "project-key",
        "mail_url": "http://127.0.0.1:18765/mcp",
        "bearer_mode": "enabled",
        "mail_env": str(tmp_path / "mail.env"),
    }

    launcher.configure_proxy(home, spec)
    config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    proxy = config["mcp_servers"]["orrery-mail"]
    environment = proxy["env"]

    assert config["model"] == spec["model"]
    assert config["model_reasoning_effort"] == spec["effort"]
    assert config["windows"]["sandbox"] == "elevated"
    assert proxy["command"] == spec["python"]
    assert proxy["required"] is True
    assert proxy["args"] == ["-X", "utf8", str(PROXY_PATH)]
    assert environment["AGENTSTACK_RUNTIME_DIR"] == spec["state"]
    assert environment["AGENTSTACK_CODEX_APP_RUNTIME_DIR"] == str(
        home / "proxy-runtime"
    )
    assert environment["AGENTSTACK_PROXY_TOKEN_FILE"] == str(state / "owner.token")
    assert environment["AGENTSTACK_PROXY_AGENT_NAME"] == spec["name"]
    assert environment["AGENTSTACK_PROJECT_KEY"] == spec["project"]
    assert environment["AGENTSTACK_MCP_URL"] == spec["mail_url"]
    assert environment["AGENTSTACK_MAIL_HTTP_BEARER_MODE"] == spec["bearer_mode"]
    assert environment["AGENTSTACK_MAIL_ENV"] == spec["mail_env"]


def test_child_environment_drops_parent_credentials_and_binds_runtimes() -> None:
    spec = {
        "home": r"C:\private\child-home",
        "path": r"C:\Windows\System32",
        "name": "BlueLake",
        "parent": "GreenCastle",
        "project": r"C:\project",
        "codex": r"C:\Tools\codex.exe",
        "python": r"C:\Tools\python.exe",
    }
    inherited = {
        "MCP_AGENT_MAIL_TOKEN": "must-not-reach-codex",
        "HTTP_BEARER_TOKEN": "must-not-reach-codex",
        "OPENAI_API_KEY": "must-not-reach-codex",
        "AGENTSTACK_PROXY_TOKEN_FILE": r"C:\unsafe\token",
        "AGENTSTACK_MCP_URL": "http://127.0.0.1:18765/mcp",
        "CHILD_REGISTRATION_TOKEN": "must-not-reach-codex",
        "PATH": r"C:\parent\bin",
        "KEEP": "yes",
    }

    result = launcher.child_environment(spec, inherited)

    assert result["KEEP"] == "yes"
    for key in (
        "MCP_AGENT_MAIL_TOKEN",
        "HTTP_BEARER_TOKEN",
        "OPENAI_API_KEY",
        "AGENTSTACK_PROXY_TOKEN_FILE",
        "AGENTSTACK_MCP_URL",
        "CHILD_REGISTRATION_TOKEN",
    ):
        assert key not in result
    assert result["CODEX_HOME"] == spec["home"]
    assert result["CODEX_SHARED_CODEX_DIR"] == spec["home"]
    assert result["AGENTSTACK_CODEX_BIN"] == spec["codex"]
    assert result["AGENTSTACK_PYTHON"] == spec["python"]
