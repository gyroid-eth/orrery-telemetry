"""The dashboard's Codex resume applies the same child launch policy as spawn_child.sh.

Before 2026-09-04 the resume path hardcoded `--ask-for-approval on-request`,
no network flag and only the vault as an extra writable root, so a resumed
agent asked for approval on every command while a freshly spawned one did not.
"""
from __future__ import annotations

import os
import shlex

import pytest

import dashboard.server as server


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
