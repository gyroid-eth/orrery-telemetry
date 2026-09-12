from __future__ import annotations

import pathlib


ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_launchd_template_exports_selected_python_to_children():
    text = (ROOT / "dashboard" / "agentdashboard.plist.template").read_text(
        encoding="utf-8"
    )
    expected = "<key>AGENTSTACK_PYTHON</key>\n    <string>__PYTHON__</string>"
    assert expected in text
