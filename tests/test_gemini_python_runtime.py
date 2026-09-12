from __future__ import annotations

import pathlib


ROOT = pathlib.Path(__file__).resolve().parent.parent
HOOKS = (
    ROOT / "hooks" / "spawn_gemini_child.sh",
    ROOT / "hooks" / "spawn_gemini_preregistered.sh",
)


def test_gemini_hooks_use_agentstack_selected_python():
    for hook in HOOKS:
        text = hook.read_text(encoding="utf-8")
        assert 'PYTHON_BIN="${AGENTSTACK_PYTHON:-python3}"' in text, hook
        assert "\npython3 -" not in text, hook
        assert '"$PYTHON_BIN" "$MAIL_HELPER"' in text, hook
        assert '$(printf \'%q\' "$PYTHON_BIN") $(printf \'%q\' "$STREAM_HELPER")' in text, hook


def test_dashboard_launchd_exports_selected_python_to_children():
    template = (ROOT / "dashboard" / "agentdashboard.plist.template").read_text(
        encoding="utf-8"
    )
    installer = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "<key>AGENTSTACK_PYTHON</key>\n    <string>__PYTHON__</string>" in template
    assert '"__PYTHON__": "$PYTHON_BIN"' in installer
