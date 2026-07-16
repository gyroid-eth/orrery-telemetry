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
        "SessionStart",
        "SubagentStart",
        "UserPromptSubmit",
        "PostToolUse",
        "Stop",
        "SubagentStop",
    }
    for groups in hooks.values():
        assert len(groups) == 1
        handler = groups[0]["hooks"][0]
        assert handler["type"] == "command"
        assert handler["async"] is False
        assert handler["timeoutSec"] == 1
        assert "$PLUGIN_ROOT/../src/agentstack_codex_app/hook_entry.py" in handler["command"]


def test_plugin_hooks_do_not_wire_cold_wake():
    text = HOOKS.read_text(encoding="utf-8").lower()
    assert "wake.py" not in text
    assert "resume" not in text


def test_plugin_manifest_wires_relative_stdio_mcp_server():
    manifest = json.loads(
        (PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["mcpServers"] == "./.mcp.json"

    config = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
    assert set(config["mcpServers"]) == {"agentstack"}
    server = config["mcpServers"]["agentstack"]
    assert server == {
        "command": "python3",
        "args": ["../src/agentstack_codex_app/mcp_server.py"],
        "cwd": ".",
    }
    assert "PLUGIN_ROOT" not in json.dumps(config)
    assert "PLUGIN_DATA" not in json.dumps(config)
