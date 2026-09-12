from __future__ import annotations

import pathlib


ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_dashboard_launchd_exports_selected_python():
    template = (ROOT / "dashboard" / "agentdashboard.plist.template").read_text(
        encoding="utf-8"
    )
    assert "<key>AGENTSTACK_PYTHON</key>" in template
    assert "<string>__PYTHON__</string>" in template
