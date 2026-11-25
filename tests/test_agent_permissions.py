"""Saved permissions of agents without a launch record: record, launch input and exec."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from hooks import agent_permissions as permissions, screen_state


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "agentstack-agent-permissions"
IDENTITY = dict(agent_id=7, project_key="/fixture/project", program="claude-code")
DIRECT = {"command": "/usr/bin/python3", "args": ["/fixture/cua_relay.py", "--", "/fixture/cua-driver", "mcp"]}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    directory = tmp_path / "runtime"
    directory.mkdir(mode=0o700)
    monkeypatch.setenv("AGENTSTACK_SCREEN_PYTHON", sys.executable)
    return directory


def _claude_json(tmp_path, servers=None, projects=None):
    path = tmp_path / "claude.json"
    path.write_text(json.dumps({"mcpServers": servers or {}, "projects": projects or {}}))
    return str(path)


def test_save_keeps_one_private_record_per_agent(runtime):
    assert permissions.read(runtime, "SampleAgent") is None
    permissions.save(runtime, "SampleAgent", computer_use="control", **IDENTITY)
    record = permissions.save(runtime, "SampleAgent", browser_use=False, **IDENTITY)
    assert (record["computer_use"], record["browser_use"]) == ("control", False)
    target = runtime / "agent-permissions" / "SampleAgent.json"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert permissions.read(runtime, "SampleAgent", agent_id=7, project_key="/fixture/project") == record
    # A later registration of the same name does not inherit the record.
    assert permissions.read(runtime, "SampleAgent", agent_id=8) is None
    cleared = permissions.save(runtime, "SampleAgent", computer_use=None, **IDENTITY)
    assert "computer_use" not in cleared and cleared["browser_use"] is False
    replaced = permissions.save(runtime, "SampleAgent", browser_use=True, **{**IDENTITY, "agent_id": 8})
    assert replaced["agent_id"] == 8 and "computer_use" not in replaced


@pytest.mark.parametrize("changes", [{}, {"computer_use": "full"}, {"browser_use": "yes"}, {"model": "x"}])
def test_save_rejects_other_values(runtime, changes):
    with pytest.raises(permissions.AgentPermissionsError):
        permissions.save(runtime, "SampleAgent", **IDENTITY, **changes)


def test_unsafe_records_are_not_read(runtime):
    permissions.save(runtime, "SampleAgent", browser_use=True, **IDENTITY)
    target = runtime / "agent-permissions" / "SampleAgent.json"
    target.chmod(0o644)
    with pytest.raises(permissions.AgentPermissionsError):
        permissions.read(runtime, "SampleAgent")
    target.chmod(0o600)
    target.write_text(json.dumps({"version": 1, "agent_name": "Other"}))
    with pytest.raises(permissions.AgentPermissionsError):
        permissions.read(runtime, "SampleAgent")
    with pytest.raises(permissions.AgentPermissionsError):
        permissions.path(runtime, "../escape")
    # Saving again replaces the unreadable record.
    assert permissions.save(runtime, "SampleAgent", browser_use=False, **IDENTITY)["browser_use"] is False


def test_browser_only_adds_the_flag_unless_the_launcher_chose(runtime, tmp_path):
    record = permissions.save(runtime, "SampleAgent", browser_use=True, **IDENTITY)
    claude = _claude_json(tmp_path)
    assert permissions.claude_launch(runtime, record, ["claude", "--channels", "plugin:x"], cwd=None,
                                     claude_json=claude) == (["--chrome"], {}, None)
    assert permissions.claude_launch(runtime, record, ["claude", "--no-chrome"], cwd=None,
                                     claude_json=claude) == ([], {}, None)


def test_computer_use_uses_the_gated_relay_and_denies_direct_paths(runtime, tmp_path):
    record = permissions.save(runtime, "SampleAgent", computer_use="view", **IDENTITY)
    work = str(tmp_path / "work")
    claude = _claude_json(tmp_path, servers={"cua-driver": DIRECT, "semantic": {"command": "search"}},
                          projects={work: {"mcpServers": {"desktop": {"command": "cua-driver", "args": ["mcp"]}}}})
    arguments, servers, generation = permissions.claude_launch(
        runtime, record, ["claude", "--resume", "sid"], cwd=work, claude_json=claude,
        origin="http://127.0.0.1:8899", platform="darwin")
    assert list(servers) == ["orrery-screen"]
    assert servers["orrery-screen"]["args"] == ["-I", "-m", "agentstack_mail.screen_mcp"]
    assert arguments == ["--settings", json.dumps({"deniedMcpServers": [
        {"serverName": "computer-use"}, {"serverName": "cua-driver"}, {"serverName": "desktop"}]},
        separators=(",", ":")), "--disallowed-tools", "mcp__computer-use"]
    store = screen_state.Store(str(runtime))
    (launch,) = [item for item in store.list() if item["agent"] == "SampleAgent"]
    assert (launch["generation"], launch["computer_use"], launch["project"]) == (
        generation, "view", "/fixture/project")
    permissions.revoke(runtime, record, generation)
    assert not [item for item in store.list() if item["agent"] == "SampleAgent"]
    linux, _servers, _generation = permissions.claude_launch(
        runtime, record, ["claude"], cwd=None, claude_json=_claude_json(tmp_path), platform="linux")
    assert linux == []


@pytest.mark.parametrize("given", [["--settings", "{}"], ["--settings={}"], ["--strict-mcp-config"]])
def test_computer_use_refuses_a_launcher_that_owns_the_same_controls(runtime, tmp_path, given):
    record = permissions.save(runtime, "SampleAgent", computer_use="control", **IDENTITY)
    with pytest.raises(permissions.AgentPermissionsError):
        permissions.claude_launch(runtime, record, ["claude", *given], cwd=None, claude_json=_claude_json(tmp_path))


def test_apply_writes_the_server_file_and_marks_the_launch(runtime, tmp_path):
    assert permissions.apply_to_claude(runtime, "SampleAgent", ["claude"], launcher="exec", cwd=None,
                                       claude_json=_claude_json(tmp_path)) == ([], None, None)
    permissions.save(runtime, "SampleAgent", computer_use="control", browser_use=True, **IDENTITY)
    arguments, record, generation = permissions.apply_to_claude(
        runtime, "SampleAgent", ["claude"], launcher="exec", cwd=None, claude_json=_claude_json(tmp_path),
        agent_id=7, platform="linux")
    config = Path(arguments[1])
    assert arguments[0] == "--mcp-config" and arguments[2:] == ["--chrome"]
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert list(json.loads(config.read_text())["mcpServers"]) == ["orrery-screen"]
    applied = permissions.read(runtime, "SampleAgent")["applied"]
    assert (applied["launcher"], applied["generation"]) == ("exec", generation)
    # A save after the launch read its record is not marked as applied.
    permissions.save(runtime, "SampleAgent", browser_use=False, **IDENTITY)
    permissions.mark_applied(runtime, "SampleAgent", record, "exec", "stale")
    assert permissions.read(runtime, "SampleAgent")["applied"]["generation"] == generation


def _fake_claude(tmp_path):
    record = tmp_path / "argv.json"
    binary = tmp_path / "bin" / "claude"
    binary.parent.mkdir(exist_ok=True)
    binary.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > " + str(record) + "\n")
    binary.chmod(0o755)
    return binary, record


def _run_cli(tmp_path, runtime, *args):
    env = {**os.environ, "AGENTSTACK_RUNTIME_DIR": str(runtime), "AGENTSTACK_HOOKS_DIR": str(ROOT / "hooks"),
           "AGENTSTACK_CLAUDE_JSON": _claude_json(tmp_path), "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}
    return subprocess.run([sys.executable, str(CLI), *args], env=env, capture_output=True, text=True,
                          timeout=30, cwd=tmp_path)


def test_exec_appends_saved_arguments_and_keeps_the_command(runtime, tmp_path):
    binary, record = _fake_claude(tmp_path)
    command = ["exec", "SampleAgent", "--", "claude", "--model", "sample", "--channels", "plugin:x"]
    assert _run_cli(tmp_path, runtime, *command).returncode == 0
    assert record.read_text().split("\n")[:-1] == ["--model", "sample", "--channels", "plugin:x"]
    permissions.save(runtime, "SampleAgent", browser_use=False, **IDENTITY)
    assert _run_cli(tmp_path, runtime, *command).returncode == 0
    assert record.read_text().split("\n")[:-1] == ["--model", "sample", "--channels", "plugin:x", "--no-chrome"]
    shown = _run_cli(tmp_path, runtime, "show", "SampleAgent")
    assert json.loads(shown.stdout)["browser_use"] is False
    other = tmp_path / "bin" / "other"
    other.write_text("#!/bin/sh\nexit 0\n")
    other.chmod(0o755)
    refused = _run_cli(tmp_path, runtime, "exec", "SampleAgent", "--", "other")
    assert refused.returncode != 0 and "Claude Code commands only" in refused.stderr
