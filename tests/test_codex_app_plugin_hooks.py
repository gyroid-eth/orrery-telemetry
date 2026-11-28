from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "integrations" / "codex_app" / "plugin" / "hooks" / "hooks.json"
PLUGIN = ROOT / "integrations" / "codex_app" / "plugin"


def test_plugin_hooks_wire_every_p1_telemetry_event():
    payload = json.loads(HOOKS.read_text(encoding="utf-8"))
    hooks = payload["hooks"]
    assert set(hooks) == {
        "PreToolUse",
        "SessionStart",
        "SubagentStart",
        "UserPromptSubmit",
        "PostToolUse",
        "Stop",
        "SubagentStop",
    }
    for event, groups in hooks.items():
        assert len(groups) == (2 if event == "PreToolUse" else 1)
        handler = groups[0]["hooks"][0]
        assert handler["type"] == "command"
        assert handler["async"] is False
        # SessionStart gives the recorder time to bind; interactive hooks keep the 1-second budget.
        expected_timeout = 30 if event == "PreToolUse" else 5 if event == "SessionStart" else 1
        assert handler["timeoutSec"] == expected_timeout
        if event == "PreToolUse":
            assert groups[0]["matcher"] == "^apply_patch$"
            assert handler["command"] == '"$PLUGIN_ROOT/scripts/run-reservation-guard.sh"'
            assert handler["timeout"] == 30
        else:
            assert handler["command"] == '"$PLUGIN_ROOT/scripts/run-hook.sh"'


def test_plugin_shell_hook_is_synchronous_and_uses_the_core_key_guard():
    group = json.loads(HOOKS.read_text())["hooks"]["PreToolUse"][1]
    assert group["matcher"] == "*"
    assert group["hooks"] == [{"type": "command", "command": '\"$PLUGIN_ROOT/scripts/run-key-read-guard.sh\"',
                               "async": False, "timeoutSec": 5, "timeout": 5}]
    assert (PLUGIN / "scripts/run-key-read-guard.sh").stat().st_mode & 0o111


def test_plugin_hooks_do_not_wire_cold_wake():
    text = HOOKS.read_text(encoding="utf-8").lower()
    assert "wake.py" not in text
    assert "resume" not in text


def test_hook_runner_keeps_cli_recorder_separate_from_desktop_bridge():
    runner = (PLUGIN / "scripts" / "run-hook.sh").read_text(encoding="utf-8")
    assert '$PLUGIN_ROOT/scripts/record-codex-session-index.py' in runner
    assert "AGENTSTACK_HOOKS_DIR" not in runner
    assert 'AGENTSTACK_CODEX_LAUNCH_BINDING' not in runner
    assert 'agentstack_codex_app/hook_entry.py' in runner


def test_plugin_manifest_wires_relative_stdio_mcp_server():
    manifest = json.loads(
        (PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["mcpServers"] == "./.mcp.json"

    config = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
    assert set(config["mcpServers"]) == {"agentstack"}
    server = config["mcpServers"]["agentstack"]
    assert server == {
        "command": "/bin/bash",
        "args": ["scripts/run-mcp.sh"],
        "cwd": ".",
    }
    assert "PLUGIN_ROOT" not in json.dumps(config)
    assert "PLUGIN_DATA" not in json.dumps(config)
    for runner in (
        "run-hook.sh",
        "record-codex-session-index.py",
        "run-mcp.sh",
    ):
        path = PLUGIN / "scripts" / runner
        assert path.is_file()
        assert path.stat().st_mode & 0o111


def test_shell_hook_fast_path_only_trusts_an_unambiguous_mcp_header(tmp_path):
    import os
    import subprocess

    core = tmp_path / "core"
    hooks = core / "hooks"
    hooks.mkdir(parents=True)
    expected = tmp_path / "shell-handler-input"
    guard = hooks / "deny-llm-key-read.sh"
    guard.write_text('cat > "$CAPTURE"\nexit 2\n')
    env = {
        **os.environ,
        "AGENTSTACK_HOME": str(core),
        "AGENTSTACK_LABEL_PREFIX": "org.agentstack.test-codex-shell-hook",
        "CAPTURE": str(expected),
    }
    runner = PLUGIN / "scripts/run-key-read-guard.sh"

    def run(payload):
        return subprocess.run(["/bin/bash", str(runner)], input=payload,
                              text=True, env=env, capture_output=True, timeout=5)

    canonical = json.dumps({
        "session_id": "fixture",
        "cwd": '/tmp/path with "quotes"',
        "hook_event_name": "PreToolUse",
        "tool_name": "mcp__orrery_fixture__fetch_inbox",
        "tool_input": {"cmd": "credential-looking sample"},
    })
    assert run(canonical).returncode == 0
    assert not expected.exists()

    samples = [
        '{"tool_name":"renamed_shell","tool_input":{"cmd":"printf harmless"}}',
        '{"nested":{"tool_name":"mcp__fixture__read"},"tool_name":"Bash","tool_input":{}}',
        '{"tool_name":"mcp__fixture__read","tool_name":"Bash","tool_input":{}}',
        json.dumps({"tool_name": "Bash", "tool_input": {"command": 'printf \'"tool_name":"mcp__fake"\''}}),
        '{"tool_name":"mcp__","tool_input":{}}',
    ]
    for sample in samples:
        assert run(sample).returncode == 2
        assert expected.read_text().rstrip("\n") == sample
