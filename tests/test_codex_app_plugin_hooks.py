from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "integrations" / "codex_app" / "plugin" / "hooks" / "hooks.json"


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
