"""agentstack-persistent applies the permissions saved for its agent (fake Claude, fake Mail)."""

import json
import subprocess
import sys
from pathlib import Path

from hooks import agent_permissions as permissions, screen_state
from test_persistent_agents import LAUNCHER, ROOT, _fixture, _install_claude_fixture_plugin, _write_executable


def _launch(tmp_path, *, command_extra=()):
    record = tmp_path / "claude-launch.json"
    fake_claude = _write_executable(
        tmp_path / "bin" / "claude",
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "config = json.load(open(os.environ['AGENTSTACK_PERSISTENT_MCP_CONFIG']))\n"
        f"open({str(record)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], 'config': config}}))\n",
    )
    profile, runtime, _state, env = _fixture(
        tmp_path, interaction="interactive", provider="claude",
        command=[str(fake_claude), "--channels", "plugin:dummy-channel@fixture", *command_extra])
    config_root, _plugin = _install_claude_fixture_plugin(tmp_path, profile)
    (config_root / ".claude.json").write_text(json.dumps({"mcpServers": {"cua-driver": {
        "command": "/usr/bin/python3", "args": ["/fixture/cua_relay.py", "--", "/fixture/cua-driver", "mcp"]}}}))
    env.update(AGENTSTACK_HOOKS_DIR=str(ROOT / "hooks"), AGENTSTACK_SCREEN_PYTHON=sys.executable)
    project = json.loads(profile.read_text())["project_key"]
    return profile, runtime, env, record, project


def _run(profile, env):
    return subprocess.run([str(LAUNCHER), "run", "--profile", str(profile)], env=env,
                          capture_output=True, text=True, timeout=30)


def test_without_a_record_the_launch_is_unchanged(tmp_path):
    profile, _runtime, env, record, _project = _launch(tmp_path)
    result = _run(profile, env)
    assert result.returncode == 0, result.stderr
    value = json.loads(record.read_text())
    assert value["argv"][:2] == ["--channels", "plugin:dummy-channel@fixture"]
    assert value["argv"][2] == "--mcp-config" and len(value["argv"]) == 4
    assert "orrery-screen" not in value["config"]["mcpServers"]


def test_saved_permissions_are_added_and_channels_are_kept(tmp_path):
    profile, runtime, env, record, project = _launch(tmp_path)
    permissions.save(runtime, "PersistentBot", agent_id=41, project_key=project, program="claude-code",
                     computer_use="control")
    permissions.save(runtime, "PersistentBot", agent_id=41, project_key=project, program="claude-code",
                     browser_use=True)
    result = _run(profile, env)
    assert result.returncode == 0, result.stderr
    value = json.loads(record.read_text())
    argv = value["argv"]
    assert argv[:3] == ["--channels", "plugin:dummy-channel@fixture", "--mcp-config"]
    assert "--chrome" in argv
    denied = json.loads(argv[argv.index("--settings") + 1])["deniedMcpServers"]
    assert {"serverName": "cua-driver"} in denied
    servers = value["config"]["mcpServers"]
    assert servers["orrery-screen"]["args"] == ["-I", "-m", "agentstack_mail.screen_mcp"]
    assert "cua-driver" not in servers
    applied = permissions.read(runtime, "PersistentBot")["applied"]
    (launch,) = [item for item in screen_state.Store(str(runtime)).list() if item["agent"] == "PersistentBot"]
    assert (applied["launcher"], applied["generation"]) == ("persistent", launch["generation"])
    assert launch["computer_use"] == "control"


def test_a_record_for_another_registration_is_ignored(tmp_path):
    profile, runtime, env, record, project = _launch(tmp_path)
    permissions.save(runtime, "PersistentBot", agent_id=99, project_key=project, program="claude-code",
                     browser_use=False)
    assert _run(profile, env).returncode == 0
    assert "--no-chrome" not in json.loads(record.read_text())["argv"]


def test_a_browser_flag_in_the_profile_stays_the_profiles_choice(tmp_path):
    profile, runtime, env, record, project = _launch(tmp_path, command_extra=("--no-chrome",))
    permissions.save(runtime, "PersistentBot", agent_id=41, project_key=project, program="claude-code",
                     browser_use=True)
    assert _run(profile, env).returncode == 0
    argv = json.loads(record.read_text())["argv"]
    assert "--no-chrome" in argv and "--chrome" not in argv


def test_an_unreadable_record_stops_the_start_with_a_reason(tmp_path):
    profile, runtime, env, record, project = _launch(tmp_path)
    permissions.save(runtime, "PersistentBot", agent_id=41, project_key=project, program="claude-code",
                     browser_use=True)
    (runtime / "agent-permissions" / "PersistentBot.json").chmod(0o644)
    result = _run(profile, env)
    assert result.returncode != 0 and "saved-permissions-unavailable" in result.stderr
    assert not Path(record).exists()
