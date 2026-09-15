"""The dashboard's Codex resume applies the same child launch policy as spawn_child.sh.

Before 2026-09-04 the resume path hardcoded `--ask-for-approval on-request`,
no network flag and only the vault as an extra writable root, so a resumed
agent asked for approval on every command while a freshly spawned one did not.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

import dashboard.server as server


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def policy_env(monkeypatch, tmp_path):
    for name in ("AGENTSTACK_CODEX_CHILD_APPROVAL", "AGENTSTACK_CODEX_NETWORK",
                 "AGENTSTACK_CODEX_ADD_DIRS", "AGENTSTACK_SPAWN_DIRS",
                 "AGENTSTACK_SPAWN_ROOTS", "AGENTSTACK_HOME"):
        monkeypatch.delenv(name, raising=False)
    project = tmp_path / "proj with space"
    project.mkdir()
    monkeypatch.setattr(server, "PROJECT_KEY", str(project))
    monkeypatch.setattr(server, "VAULT", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path, project


def test_resume_defaults_to_never_with_network_and_project_root(policy_env):
    tmp_path, project = policy_env
    flags = server._codex_child_launch_flags()
    assert flags.startswith("--sandbox workspace-write --ask-for-approval never")
    assert "-c sandbox_workspace_write.network_access=true" in flags
    assert f"--add-dir {shlex.quote(os.path.realpath(str(project)))}" in flags
    # The old hardcoded on-request is gone.
    assert "on-request" not in flags


def test_resume_honours_installer_settings_and_extra_roots(policy_env, monkeypatch):
    tmp_path, project = policy_env
    preset = tmp_path / "code"
    extra = tmp_path / "extra"
    preset.mkdir()
    extra.mkdir()
    monkeypatch.setenv("AGENTSTACK_CODEX_CHILD_APPROVAL", "on-failure")
    monkeypatch.setenv("AGENTSTACK_CODEX_NETWORK", "off")
    monkeypatch.setenv("AGENTSTACK_SPAWN_DIRS", f"{preset}:/does/not/exist")
    monkeypatch.setenv("AGENTSTACK_CODEX_ADD_DIRS", str(extra))
    flags = server._codex_child_launch_flags()
    assert "--ask-for-approval on-failure" in flags
    assert "network_access" not in flags
    dirs = server._codex_child_add_dirs()
    assert dirs[0] == os.path.realpath(str(project))
    assert os.path.realpath(str(preset)) in dirs
    assert dirs[-1] == os.path.realpath(str(extra))
    assert "/does/not/exist" not in dirs
    assert len(dirs) == len(set(dirs))


def test_resume_sources_the_installed_product_bootstrap(policy_env, monkeypatch):
    tmp_path, project = policy_env
    rollout = tmp_path / "rollout-session.jsonl"
    session_id = "01d13f58-8e1a-7777-a3d8-e2ba243cdb49"
    rollout.write_text(
        json.dumps({
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": str(project)},
        }) + "\n",
        encoding="utf-8",
    )
    install_home = tmp_path / "installed-agentstack"
    bootstrap = install_home / "bin" / "agentstack-codex-bootstrap"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    launched = []
    monkeypatch.setenv("AGENTSTACK_HOME", str(install_home))
    monkeypatch.setattr(server, "_codex_transcript_path", lambda _name: str(rollout))
    monkeypatch.setattr(
        server,
        "_open_terminal_tmux",
        lambda args, **kwargs: launched.append(args) or {"ok": True, "adapter": "fixture"},
    )

    result = server._do_resume_codex("BoundCodex")

    assert result["ok"] is True
    assert len(launched) == 1
    inner = launched[0][-1]
    assert f"source {shlex.quote(str(bootstrap))}" in inner
    assert "AGENTSTACK_CODEX_LAUNCH_KIND=resume" in inner
    assert ".codex/bin/codex_agent_bootstrap.sh" not in inner


def test_installed_bootstrap_creates_a_fresh_resume_generation(policy_env):
    tmp_path, project = policy_env
    install_home = tmp_path / "installed-agentstack"
    bindir = install_home / "bin"
    libdir = bindir / "lib"
    hooks = install_home / "hooks"
    libdir.mkdir(parents=True)
    hooks.mkdir(parents=True)
    bootstrap = bindir / "agentstack-codex-bootstrap"
    bootstrap.write_text(
        (ROOT / "bin" / "agentstack-codex-bootstrap").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    prepare = hooks / "prepare-codex-session-binding.py"
    prepare.write_text(
        (ROOT / "hooks" / "prepare-codex-session-binding.py").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    (libdir / "agentstack-register.sh").write_text(
        "ags_mail_load_token() { :; }\n"
        "ags_mcp_call() { return 0; }\n"
        "ags_start_mail_watcher() { :; }\n"
        "ags_register_session() {\n"
        "  AGS_REGISTERED_AGENT_NAME=BoundCodex\n"
        "  AGS_REGISTERED_AGENT_ID=73\n"
        "  return 0\n"
        "}\n"
        "ags_record_managed_agent() { :; }\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    command = (
        "AGENTSTACK_CODEX_LAUNCH_BINDING=/parent/launch.json; "
        "AGENTSTACK_CODEX_LAUNCH_ID=parent-launch; "
        f'source "{bootstrap}" "{project}" >/dev/null; '
        "status=$?; printf '%s|%s|%s\n' \"$status\" "
        '"$AGENTSTACK_CODEX_LAUNCH_BINDING" "$AGENTSTACK_CODEX_LAUNCH_ID"'
    )
    result = subprocess.run(
        ["bash", "-c", command],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path / "clean-home"),
            "AGENTSTACK_PROJECT_KEY": str(project),
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_HOOKS_DIR": str(hooks),
            "AGENTSTACK_RESERVED_IDENTITY": "1",
            "AGENTSTACK_CODEX_LAUNCH_KIND": "resume",
            "AGENT_NAME": "BoundCodex",
            "TMUX": "",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    status, launch_path, launch_id = result.stdout.strip().split("|")
    assert status == "0", result.stderr
    assert launch_path == str(runtime / "codex_launches" / "73.json")
    assert launch_id and launch_id != "parent-launch"
    launch = json.loads(Path(launch_path).read_text(encoding="utf-8"))
    assert launch["launch_kind"] == "resume"
    assert launch["agent_id"] == 73
    assert launch["claimed_session_id"] is None
    assert launch["receipt_id"] is None


@pytest.mark.parametrize("failure_mode", ["project_unset", "health_unreachable"])
def test_reserved_resume_stops_before_exec_when_binding_preconditions_fail(
    policy_env, failure_mode
):
    tmp_path, project = policy_env
    install_home = tmp_path / "installed-agentstack"
    bindir = install_home / "bin"
    libdir = bindir / "lib"
    libdir.mkdir(parents=True)
    bootstrap = bindir / "agentstack-codex-bootstrap"
    bootstrap.write_text(
        (ROOT / "bin" / "agentstack-codex-bootstrap").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    health_status = 1 if failure_mode == "health_unreachable" else 0
    (libdir / "agentstack-register.sh").write_text(
        "ags_mail_load_token() { :; }\n"
        f"ags_mcp_call() {{ return {health_status}; }}\n"
        "ags_start_mail_watcher() { :; }\n"
        "ags_record_managed_agent() { :; }\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    command = (
        f'source "{bootstrap}" "{project}" >/dev/null '
        "&& printf REACHED_CODEX_EXEC_BRANCH"
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "clean-home"),
        "AGENTSTACK_PROJECT_KEY": (
            "" if failure_mode == "project_unset" else str(project)
        ),
        "AGENTSTACK_RUNTIME_DIR": str(runtime),
        "AGENTSTACK_RESERVED_IDENTITY": "1",
        "AGENTSTACK_CODEX_LAUNCH_KIND": "resume",
        "AGENT_NAME": "BoundCodex",
        "TMUX": "",
    }

    result = subprocess.run(
        ["bash", "-c", command],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "REACHED_CODEX_EXEC_BRANCH" not in result.stdout
    assert not (runtime / "codex_launches").exists()
