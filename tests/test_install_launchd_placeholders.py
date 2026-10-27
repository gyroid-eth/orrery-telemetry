"""Exercise the installer plist writer without installing or calling launchctl."""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "dashboard/agentdashboard.plist.template"
PLACEHOLDER = re.compile(r"__[A-Z0-9_]+__")


def _renderer(writer: str) -> str:
    if writer in ("installer", "systemd"):
        text = (ROOT / "scripts/install.sh").read_text()
        if writer == "systemd":
            start = text.index("render_systemd_unit() {")
            return text[start:text.index("\nstart_service() {", start)]
        return text[text.index("render_launchd_plist() {"):text.index("render_systemd_unit() {")]
    text = (ROOT / "dashboard/agentctl.sh").read_text()
    return text[text.index("sed_escape() {"):text.index("background_pid() {")]


def _render(tmp_path: Path, writer: str, mode: str | None, config: str | None,
            settings: dict[str, str] | None = None) -> Path:
    body = _renderer(writer)
    home = tmp_path / writer
    home.mkdir()
    output = (home / ".config/systemd/user/test.service" if writer == "systemd"
              else home / "Library/LaunchAgents/test.plist")
    # Invoke just the real writer, never the installer's or controller's service
    # entry point. No real env.sh, service label, launchctl, or dashboard is used.
    names = re.findall(r"\$(?:\{)?([A-Z][A-Z0-9_]*)", body)
    env = {name: "" for name in names}
    env.update({
        "HOME": str(home), "PATH": os.environ["PATH"],
        "REPO_ROOT": str(ROOT), "LABEL": "test", "LABEL_PREFIX": "test",
        "DRY_RUN": "false", "PYTHON_BIN": sys.executable, "PYTHON": sys.executable,
        "DASHBOARD_DIR": str(home / "dashboard"), "HERE": str(home / "dashboard"),
        "PLIST_DST": str(output), "PLIST_TEMPLATE": str(TEMPLATE),
    })
    for key, value in (("AGENTSTACK_RUNTIME_MODE", mode),
                       ("AGENTSTACK_GLOBAL_SERVER_CONFIG", config)):
        env.pop(key, None)
        if value is not None:
            env[key] = value
    env.update(settings or {})
    call = {"installer": "render_launchd_plist", "controller": "render_plist",
            "systemd": "render_systemd_unit"}[writer]
    subprocess.run(["/bin/bash", "-euc", "plan() { :; }\n" + body + "\n" + call],
                   env=env, text=True, capture_output=True, check=True, timeout=20)
    return output


@pytest.mark.parametrize("writer", ["installer", "controller"])
def test_replacement_set_matches_every_template_placeholder(writer):
    template = set(PLACEHOLDER.findall(TEMPLATE.read_text()))
    replacements = set(PLACEHOLDER.findall(_renderer(writer)))
    assert replacements == template, {
        "missing": sorted(template - replacements),
        "stale": sorted(replacements - template),
    }


def test_numeric_placeholder_added_to_template_is_rejected(tmp_path, monkeypatch):
    template = plistlib.loads(TEMPLATE.read_bytes())
    template["EnvironmentVariables"]["AGENTSTACK_FUTURE_SETTING2"] = "__FUTURE_SETTING2__"
    mutated = tmp_path / "numeric-placeholder.plist.template"
    mutated.write_bytes(plistlib.dumps(template))
    monkeypatch.setattr(sys.modules[__name__], "TEMPLATE", mutated)
    for writer in ("installer", "controller"):
        with pytest.raises(AssertionError, match="__FUTURE_SETTING2__"):
            test_replacement_set_matches_every_template_placeholder(writer)


@pytest.mark.parametrize("mode,config", [
    (None, None), ("legacy", ""), ("global", "/fixture/server.json"),
    ("global", '/fixture/Research & <Notes>/"server"\\config\nnext.json'),
])
@pytest.mark.parametrize("writer", ["installer", "controller"])
def test_plist_writers_render_all_tokens_and_preserve_runtime_values(tmp_path, mode, config, writer):
    output = _render(tmp_path, writer, mode, config)
    assert not PLACEHOLDER.findall(output.read_text()), "unexpanded launchd placeholders"
    installed = plistlib.loads(output.read_bytes())["EnvironmentVariables"]
    assert installed["AGENTSTACK_RUNTIME_MODE"] == (mode or "legacy")
    assert installed["AGENTSTACK_GLOBAL_SERVER_CONFIG"] == (config or "")



@pytest.mark.parametrize("mode,config", [(None, None), ("legacy", ""),
                                       ("global", "/fixture/server.json")])
def test_systemd_writer_covers_the_template_environment_and_has_no_tokens(tmp_path, mode, config):
    output = _render(tmp_path, "systemd", mode, config).read_text()
    assert not PLACEHOLDER.findall(output), "unexpanded systemd placeholders"
    values = dict(re.findall(r'^Environment="([^=]+)=(.*)"$', output, re.MULTILINE))
    template_env = plistlib.loads(TEMPLATE.read_bytes())["EnvironmentVariables"]
    assert template_env.keys() <= values.keys()
    assert values["AGENTSTACK_RUNTIME_MODE"] == (mode or "legacy")
    assert values["AGENTSTACK_GLOBAL_SERVER_CONFIG"] == (config or "")


@pytest.mark.parametrize("writer,escaped", [
    ("installer", False), ("controller", False), ("systemd", False),
    ("installer", True), ("controller", True),
])
def test_child_and_profile_settings_survive_every_service_writer(tmp_path, writer, escaped):
    path = '/fixture/Research & <Notes>/"quoted"\\next\npath' if escaped else '/fixture/config'
    expected = {
        "AGENTSTACK_SPAWN_DIRS": path + '/spawn',
        "AGENTSTACK_SPAWN_ROOTS": path + '/roots',
        "AGENTSTACK_CODEX_CHILD_APPROVAL": "on-request",
        "AGENTSTACK_CODEX_NETWORK": "restricted",
        "AGENTSTACK_CODEX_ADD_DIRS": path + '/add-dirs',
        "AGENTSTACK_CHILD_RESUME_RETENTION_DAYS": "17",
        "AGENTSTACK_CODEX_BIN": path + '/codex',
        "AGENTSTACK_PORTRAITS_DIR": path + '/portraits',
        "AGENTSTACK_CUSTOM_PORTRAITS": path + '/custom',
        "AGENTSTACK_CODEX_MODELS": "codex-fixture-1,codex-fixture-2",
        "AGENTSTACK_CODEX_DEFAULT_MODEL": "gpt-5.6-sol",
        "AGENTSTACK_CODEX_DEFAULT_EFFORT": "high",
        "AGENTSTACK_PERSISTENT_PROFILES_DIR": path + '/profiles',
    }
    settings = dict(expected)
    settings.update({key.removeprefix("AGENTSTACK_") + "_SETTING": value
                     for key, value in expected.items()})
    settings["PERSISTENT_PROFILES_DIR"] = expected["AGENTSTACK_PERSISTENT_PROFILES_DIR"]
    output = _render(tmp_path, writer, "legacy", "", settings)
    assert not PLACEHOLDER.findall(output.read_text())
    if writer == "systemd":
        values = dict(re.findall(r'^Environment="([^=]+)=(.*)"$', output.read_text(), re.MULTILINE))
    else:
        values = plistlib.loads(output.read_bytes())["EnvironmentVariables"]
    assert {key: values[key] for key in expected} == expected
