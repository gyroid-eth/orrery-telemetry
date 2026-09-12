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


def test_systemd_unit_exports_installer_selected_python_to_dashboard_children():
    # Linux / WSL2 render a systemd user unit instead of the plist; the same
    # handoff has to be present there or the fix only covers macOS.
    installer = INSTALLER.read_text(encoding="utf-8")
    unit_block = installer[installer.index('render systemd user unit $unit"') :]
    unit_block = unit_block[: unit_block.index("WantedBy=default.target")]
    assert '"AGENTSTACK_PYTHON": "$PYTHON_BIN",' in unit_block
