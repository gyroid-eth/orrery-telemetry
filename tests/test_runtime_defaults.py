"""Regression coverage for install-root runtime defaults and launcher guards."""

from __future__ import annotations

import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import time


ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALL_STATE_SAMPLE = ROOT / "scripts" / "install-state.sample.json"


def _fake_systemctl() -> str:
    """Start/stop the installed dashboard for isolated installer tests."""
    return """#!/bin/sh
case "$2" in
  show-environment|daemon-reload)
    exit 0
    ;;
  enable)
    nohup "$AGENTSTACK_TEST_PYTHON" \
      "$AGENTSTACK_HOME/dashboard/service_runner.py" >/dev/null 2>&1 &
    echo $! > "$AGENTSTACK_TEST_SERVICE_PID"
    ;;
  disable)
    if [ -f "$AGENTSTACK_TEST_SERVICE_PID" ]; then
      kill "$(sed -n '1p' "$AGENTSTACK_TEST_SERVICE_PID")" 2>/dev/null || true
    fi
    ;;
esac
exit 0
"""


def _stop_fake_dashboard(env: dict[str, str]) -> None:
    pidfile = pathlib.Path(env["AGENTSTACK_TEST_SERVICE_PID"])
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        pid = 0
    if pid > 1:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + 5
    port = int(env["AGENTSTACK_PORT"])
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.1)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                pidfile.unlink(missing_ok=True)
                return
        time.sleep(0.05)
    raise AssertionError(f"fake dashboard did not release port {port}")


def _normalize_sample_paths(value, manifest):
    """Map an isolated manifest's dynamic roots to the sample's Alice paths."""
    install_dir = pathlib.Path(manifest["install_dir"])
    replacements = (
        (manifest["repo_root"], "/home/alice/src/claude-agent-stack"),
        (manifest["env"]["AGENTSTACK_PROJECT_KEY"], "/home/alice/project"),
        (str(install_dir.parent), "/home/alice"),
    )
    if isinstance(value, dict):
        return {
            key: _normalize_sample_paths(item, manifest)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_sample_paths(item, manifest) for item in value]
    if isinstance(value, str):
        for source, target in replacements:
            value = value.replace(source, target)
    return value


def _tracked_core_payload_files() -> list[str]:
    """Return only files the core installer copies, never ignored artifacts."""
    tracked = subprocess.run(
        [
            "git", "-C", str(ROOT), "ls-files", "-z",
            "VERSION", "hooks", "skills", "dashboard", "bin", "codex", "claude",
            "integrations/codex_app/plugin", "integrations/codex_app/src",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return [path for path in tracked if path]


def _expected_owned_dirs(install_dir: pathlib.Path) -> list[str]:
    directories = {
        install_dir,
        *(install_dir / rel for rel in (
            "hooks", "skills", "dashboard", "bin", "runtime", "backups"
        )),
    }
    for relative in _tracked_core_payload_files():
        parent = (install_dir / relative).parent
        while True:
            directories.add(parent)
            if parent == install_dir:
                break
            parent = parent.parent
    return sorted(str(path) for path in directories)


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
            "from dashboard.server import ANNOT_PATH; print(ANNOT_PATH)",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == str(
        tmp_path / ".agentstack" / "runtime" / "annotations.json"
    )

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
    roots = ("hooks", "bin", "scripts", "dashboard", "integrations", "claude", "codex")
    offenders = []
    # Only tracked files: dashboard/logs/*.log and other gitignored runtime
    # output live under these roots once the dashboard has run, and they
    # legitimately contain historical paths.
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", ".env.example", *roots],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    paths = [ROOT / rel for rel in tracked if rel]
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


def test_isolated_installer_migrates_annotations_and_matches_manifest_sample(tmp_path):
    env = _clean_env(tmp_path / "home")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name, body in {
        "systemctl": _fake_systemctl(),
        "tmux": "#!/bin/sh\nexit 0\n",
        "uname": "#!/bin/sh\necho Linux\n",
        "uv": "#!/bin/sh\nexit 0\n",
    }.items():
        command = fake_bin / name
        command.write_text(body, encoding="utf-8")
        command.chmod(0o755)

    home = pathlib.Path(env["HOME"])
    install_dir = home / ".agentstack"
    user_skill = home / ".claude" / "skills" / "user-owned" / "SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("---\nname: user-owned\n---\n", encoding="utf-8")
    legacy_path = install_dir / "dashboard" / "annotations.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_log = install_dir / "dashboard" / "dashboard.log"
    legacy_log.write_text("legacy dashboard crash\n", encoding="utf-8")
    legacy_data = {
        "WiseFaraday": {"role": "legacy", "emoji": "", "group": "runtime"}
    }
    legacy_path.write_text(json.dumps(legacy_data), encoding="utf-8")
    mail_dir = home / "mcp_agent_mail"
    (mail_dir / ".git").mkdir(parents=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    project_dir = home / "project"
    project_dir.mkdir()
    env.update({
        "PATH": f"{fake_bin}:{env['PATH']}",
        "AGENTSTACK_HOME": str(install_dir),
        "AGENTSTACK_MAIL_DIR": str(mail_dir),
        "AGENTSTACK_MAIL_HOME": str(home / ".mcp_agent_mail"),
        "AGENTSTACK_MAIL_DB": str(mail_dir / "storage.sqlite3"),
        "AGENTSTACK_MAIL_ENV": str(mail_dir / ".env"),
        "AGENTSTACK_SIGNALS_DIR": str(home / ".mcp_agent_mail" / "signals"),
        "AGENTSTACK_PORT": str(port),
        "AGENTSTACK_LABEL_PREFIX": "org.agentstack",
        "AGENTSTACK_PROJECT_KEY": str(project_dir),
        "AGENTSTACK_PROTECTED_ROOTS": str(project_dir),
        "AGENTSTACK_DELIVERABLE_ROOTS": "",
        "AGENTSTACK_MCP_URL": "http://127.0.0.1:8765/mcp",
        "AGENTSTACK_TERMINAL": "auto",
        "AGENTSTACK_TEST_PYTHON": sys.executable,
        "AGENTSTACK_TEST_SERVICE_PID": str(tmp_path / "dashboard-service.pid"),
    })

    install_command = ["bash", str(ROOT / "scripts" / "install.sh")]
    install_result = subprocess.run(
        install_command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "dashboard healthy:" in install_result.stdout

    runtime_path = install_dir / "runtime" / "annotations.json"
    runtime_log = install_dir / "runtime" / "dashboard.log"
    assert json.loads(runtime_path.read_text(encoding="utf-8")) == legacy_data
    assert runtime_log.read_text(encoding="utf-8").startswith(
        "legacy dashboard crash\n"
    )
    assert not legacy_path.exists()
    assert not legacy_log.exists()

    # Reinstall must keep every old operational log out of the payload tree,
    # even when the canonical and first legacy migration targets already exist.
    _stop_fake_dashboard(env)
    legacy_log.write_text("second legacy dashboard crash\n", encoding="utf-8")
    subprocess.run(
        install_command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    runtime_legacy_log = install_dir / "runtime" / "dashboard.legacy.log"
    assert runtime_legacy_log.read_text(encoding="utf-8").startswith(
        "second legacy dashboard crash\n"
    )
    assert not legacy_log.exists()

    _stop_fake_dashboard(env)
    legacy_log.write_text("third legacy dashboard crash\n", encoding="utf-8")
    subprocess.run(
        install_command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    runtime_legacy_log_1 = install_dir / "runtime" / "dashboard.legacy.1.log"
    assert runtime_legacy_log_1.read_text(encoding="utf-8").startswith(
        "third legacy dashboard crash\n"
    )
    assert not legacy_log.exists()

    manifest = json.loads(
        (install_dir / "install-state.json").read_text(encoding="utf-8")
    )
    assert str(install_dir / "runtime") in manifest["retained_paths"]
    assert str(install_dir / "runtime") in manifest["purge_paths"]
    assert str(legacy_path) not in manifest["owned_files"]
    expected_payload_files = {
        str(install_dir / relative)
        for relative in _tracked_core_payload_files()
    }
    assert expected_payload_files <= set(manifest["owned_files"])
    assert str(install_dir / "VERSION") in manifest["owned_files"]
    assert str(
        install_dir / "integrations/codex_app/plugin/scripts/run-mcp.sh"
    ) in manifest["owned_files"]
    assert str(
        install_dir / "integrations/codex_app/src/agentstack_codex_app/mcp_server.py"
    ) in manifest["owned_files"]
    expected_skill_links = [
        {
            "path": str(home / ".claude" / "skills" / name),
            "target": str(install_dir / "skills" / name),
        }
        for name in ("delegate", "log")
    ]
    assert manifest["skill_links"] == expected_skill_links
    for record in expected_skill_links:
        link = pathlib.Path(record["path"])
        assert link.is_symlink()
        assert link.resolve() == pathlib.Path(record["target"])
        assert record["path"] in manifest["owned_files"]
    assert set(_expected_owned_dirs(install_dir)) <= set(manifest["owned_dirs"])
    systemd_unit = (
        home / ".config" / "systemd" / "user" / "org.agentstack.agentdashboard.service"
    ).read_text(encoding="utf-8")
    exec_start = next(
        line for line in systemd_unit.splitlines() if line.startswith("ExecStart=")
    )
    assert exec_start.split()[-1] == str(install_dir / "dashboard" / "service_runner.py")
    assert "Restart=always" in systemd_unit
    assert (
        f'Environment="AGENTSTACK_DASHBOARD_LOG={install_dir}/runtime/dashboard.log"'
        in systemd_unit
    )

    sample = json.loads(INSTALL_STATE_SAMPLE.read_text(encoding="utf-8"))
    assert set(sample) == set(manifest)
    assert set(sample["env"]) == set(manifest["env"])

    normalized_env = _normalize_sample_paths(manifest["env"], manifest)
    normalized_env["AGENTSTACK_PORT"] = "8770"
    assert normalized_env == sample["env"]
    for key in ("retained_paths", "purge_paths", "notes", "services", "skill_links"):
        assert _normalize_sample_paths(manifest[key], manifest) == sample[key]
    normalized_expected_dirs = _normalize_sample_paths(
        _expected_owned_dirs(install_dir), manifest
    )
    assert normalized_expected_dirs == sample["owned_dirs"]

    normalized_owned = set(_normalize_sample_paths(manifest["owned_files"], manifest))
    sample_owned = set(sample["owned_files"])
    # Tier0 does not perform the sample's Tier1 settings merge.
    sample_owned.remove("/home/alice/.agentstack/runtime/settings-merge-result.json")
    assert sample_owned <= normalized_owned

    token_path = install_dir / "runtime" / "agent_token_WiseFaraday"
    token_path.write_text("retained-token\n", encoding="utf-8")

    subprocess.run(
        [
            "bash",
            str(install_dir / "bin" / "agentstack-uninstall"),
            "--install-dir",
            str(install_dir),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    _stop_fake_dashboard(env)
    assert json.loads(runtime_path.read_text(encoding="utf-8")) == legacy_data
    assert token_path.read_text(encoding="utf-8") == "retained-token\n"
    remaining = {
        str(path.relative_to(install_dir))
        for path in install_dir.rglob("*")
    }
    assert remaining == {
        "runtime",
        "runtime/annotations.json",
        "runtime/agent_token_WiseFaraday",
        "runtime/dashboard.log",
        "runtime/dashboard.legacy.log",
        "runtime/dashboard.legacy.1.log",
    }
    assert not (install_dir / "VERSION").exists()
    assert not (install_dir / "integrations").exists()
    assert not (home / ".claude" / "skills" / "delegate").exists()
    assert not (home / ".claude" / "skills" / "log").exists()
    claude_remaining = {
        str(path.relative_to(home / ".claude"))
        for path in (home / ".claude").rglob("*")
    }
    assert claude_remaining == {
        "skills", "skills/user-owned", "skills/user-owned/SKILL.md"
    }


def test_install_state_sample_settings_merge_matches_generator(tmp_path):
    sample = json.loads(INSTALL_STATE_SAMPLE.read_text(encoding="utf-8"))
    home = tmp_path / "home"
    settings_path = home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("{}\n", encoding="utf-8")
    result_path = tmp_path / "settings-merge-result.json"
    install_dir = home / ".agentstack"
    subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "lib" / "merge_settings.py"),
            "--settings",
            str(settings_path),
            "--template",
            str(ROOT / "hooks" / "settings.template.json"),
            "--hooks-dir",
            str(install_dir / "hooks"),
            "--bin-dir",
            str(install_dir / "bin"),
            "--skills-dir",
            str(install_dir / "skills"),
            "--backup-dir",
            str(install_dir / "backups"),
            "--result-json",
            str(result_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    generated = json.loads(result_path.read_text(encoding="utf-8"))
    generated = _normalize_sample_paths(
        generated,
        {
            "install_dir": str(install_dir),
            "repo_root": str(ROOT),
            "env": {"AGENTSTACK_PROJECT_KEY": str(tmp_path / "project")},
        },
    )
    generated["before_sha256"] = "example-before-sha256"
    generated["after_sha256"] = "example-after-sha256"
    generated["backup"] = sample["settings_merge"]["backup"]

    assert generated == sample["settings_merge"]
    assert sample["backups"] == [sample["settings_merge"]["backup"]]
    assert sample["settings_backups"] == sample["backups"]


def test_installer_preserves_conflicting_user_skill(tmp_path):
    env = _clean_env(tmp_path / "home")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name, body in {
        "systemctl": _fake_systemctl(),
        "tmux": "#!/bin/sh\nexit 0\n",
        "uname": "#!/bin/sh\necho Linux\n",
        "uv": "#!/bin/sh\nexit 0\n",
    }.items():
        command = fake_bin / name
        command.write_text(body, encoding="utf-8")
        command.chmod(0o755)

    home = pathlib.Path(env["HOME"])
    install_dir = home / ".agentstack"
    user_delegate = home / ".claude" / "skills" / "delegate" / "SKILL.md"
    user_delegate.parent.mkdir(parents=True)
    original = "---\nname: delegate\n---\n\nUser-owned delegate.\n"
    user_delegate.write_text(original, encoding="utf-8")
    mail_dir = home / "mcp_agent_mail"
    (mail_dir / ".git").mkdir(parents=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    project_dir = home / "project"
    project_dir.mkdir()
    env.update({
        "PATH": f"{fake_bin}:{env['PATH']}",
        "AGENTSTACK_HOME": str(install_dir),
        "AGENTSTACK_MAIL_DIR": str(mail_dir),
        "AGENTSTACK_MAIL_HOME": str(home / ".mcp_agent_mail"),
        "AGENTSTACK_PORT": str(port),
        "AGENTSTACK_PROJECT_KEY": str(project_dir),
        "AGENTSTACK_TERMINAL": "none",
        "AGENTSTACK_TEST_PYTHON": sys.executable,
        "AGENTSTACK_TEST_SERVICE_PID": str(tmp_path / "dashboard-service.pid"),
    })

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install.sh")],
        cwd=ROOT, env=env, text=True, capture_output=True, check=True,
    )
    assert "dashboard healthy:" in result.stdout
    assert "already exists; leaving it untouched" in result.stderr
    assert user_delegate.read_text(encoding="utf-8") == original
    assert not user_delegate.parent.is_symlink()
    manifest = json.loads((install_dir / "install-state.json").read_text(encoding="utf-8"))
    assert all(record["path"] != str(user_delegate.parent) for record in manifest["skill_links"])
    log_link = home / ".claude" / "skills" / "log"
    assert log_link.is_symlink()
    user_log = home / ".claude" / "skills" / "user-log"
    user_log.mkdir()
    log_link.unlink()
    log_link.symlink_to(user_log, target_is_directory=True)

    uninstall = subprocess.run(
        [
            "bash", str(install_dir / "bin" / "agentstack-uninstall"),
            "--install-dir", str(install_dir),
        ],
        cwd=ROOT, env=env, text=True, capture_output=True, check=True,
    )
    _stop_fake_dashboard(env)
    assert user_delegate.read_text(encoding="utf-8") == original
    assert "kept retargeted skill link" in uninstall.stderr
    assert log_link.is_symlink()
    assert log_link.resolve() == user_log


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
