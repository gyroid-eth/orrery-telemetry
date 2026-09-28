"""Model overrides survive service boundaries without managing Claude profiles."""
from pathlib import Path
import os
import plistlib
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
KEY = "AGENTSTACK_CLAUDE_MODELS"


@pytest.mark.parametrize("mode", ["inherit", "replace", "clear"])
def test_installer_preserves_replaces_or_explicitly_clears_settings(tmp_path, mode):
    text = (ROOT / "scripts/install.sh").read_text()
    start = text.index('if [[ -z "${AGENTSTACK_CLAUDE_MODELS+x}" ]]')
    end = text.index('\nHOOKS_DIR=', start)
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    if mode != "inherit":
        env[KEY] = "replacement" if mode == "replace" else ""
    setup = '''CLAUDE_MODELS_SETTING="${AGENTSTACK_CLAUDE_MODELS:-}"
CODEX_MODELS_SETTING="codex-fixture"
INSTALL_DIR="/unused-fixture"
agentstack_installed_env_value() { printf '%s' "old-$1"; }
'''
    command = setup + text[start:end] + '\nprintf "%s\\n" "$AGENTSTACK_CLAUDE_MODELS"'
    result = subprocess.run(["/bin/bash", "-eu", "-c", command], env=env, text=True, capture_output=True, check=True)
    assert result.stdout.splitlines() == ["old-" + KEY if mode == "inherit" else env[KEY]]


def test_new_setting_is_data_not_interpolated_python_source():
    text = (ROOT / "scripts/install.sh").read_text()
    assert text.count(f'"{KEY}": os.environ.get("{KEY}", "")') == 3
    assert not re.search(r'": "\$CLAUDE_MODELS_SETTING"', text)


@pytest.mark.parametrize("profile", [None, "", "/fixture/profile"])
def test_controller_exports_models_without_changing_ambient_profile(tmp_path, profile):
    text = (ROOT / "dashboard/agentctl.sh").read_text()
    start = text.index('export_background_env() {')
    end = text.index('\nstart_background()', start)
    command = text[start:end] + '\nCLAUDE_MODELS_SETTING="claude-test-9"\nexport_background_env\nprintf "%s\\n%s\\n" "$AGENTSTACK_CLAUDE_MODELS" "${CLAUDE_CONFIG_DIR-unset}"'
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    if profile is not None:
        env["CLAUDE_CONFIG_DIR"] = profile
    result = subprocess.run(["/bin/bash", "-e", "-c", command], env=env, capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ["claude-test-9", "unset" if profile is None else profile]


@pytest.mark.parametrize("value", ["", "claude-test-9", 'invalid & "quoted" <model>', r"claude-\opus-5-5", "claude-opus-5-5\nclaude-sonnet-5"])
@pytest.mark.parametrize("writer", ["installer", "controller"])
def test_real_plist_writers_escape_once_and_do_not_manage_profiles(tmp_path, value, writer):
    if writer == "installer":
        text = (ROOT / "scripts/install.sh").read_text()
        body = text[text.index("render_launchd_plist() {"):text.index("render_systemd_unit() {")]
        command = "plan() { :; }\n" + body + "\nrender_launchd_plist"
    else:
        text = (ROOT / "dashboard/agentctl.sh").read_text()
        body = text[text.index("sed_escape() {"):text.index("background_pid() {")]
        command = body + "\nrender_plist"
    env = {key: "" for key in re.findall(r"\$([A-Z][A-Z0-9_]*)", body)}
    output = tmp_path / "Library/LaunchAgents/test.plist"
    env.update({"HOME": str(tmp_path), "PATH": os.environ["PATH"],
                "REPO_ROOT": str(ROOT), "LABEL": "test", "DRY_RUN": "false",
                "PYTHON_BIN": sys.executable, "PYTHON": sys.executable, "PLIST_DST": str(output),
                "PLIST_TEMPLATE": str(ROOT / "dashboard/agentdashboard.plist.template"),
                "CLAUDE_CONFIG_DIR": "/fixture/profile",
                "CLAUDE_MODELS_SETTING": value, KEY: value})
    subprocess.run(["/bin/bash", "-eu", "-c", command], env=env, capture_output=True, text=True, check=True)
    values = plistlib.loads(output.read_bytes())["EnvironmentVariables"]
    assert values[KEY] == value
    assert "CLAUDE_CONFIG_DIR" not in values


def test_profile_switching_is_not_part_of_model_catalog_installation():
    for path in ("scripts/install.sh", "dashboard/agentctl.sh", "hooks/spawn_child.sh",
                 "scripts/install-state.sample.json", "dashboard/agentdashboard.plist.template"):
        assert "CLAUDE_CONFIG_DIR" not in (ROOT / path).read_text(), path
