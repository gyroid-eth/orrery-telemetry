from __future__ import annotations

import pathlib
import plistlib


ROOT = pathlib.Path(__file__).resolve().parent.parent
PLIST_TEMPLATE = ROOT / "dashboard" / "agentdashboard.plist.template"
INSTALLER = ROOT / "scripts" / "install.sh"


def test_launchd_exports_installer_selected_python_to_dashboard_children():
    with PLIST_TEMPLATE.open("rb") as handle:
        plist = plistlib.load(handle)

    assert plist["ProgramArguments"][0] == "__PYTHON__"
    assert plist["EnvironmentVariables"]["AGENTSTACK_PYTHON"] == "__PYTHON__"

    installer = INSTALLER.read_text(encoding="utf-8")
    assert 'PYTHON_BIN="${AGENTSTACK_PYTHON:-}"' in installer
    assert '"__PYTHON__": "$PYTHON_BIN"' in installer
