"""Codex history-binding diagnostics in the core doctor."""

from __future__ import annotations

import json
import os
import pathlib
import shlex
import subprocess
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCTOR = ROOT / "scripts" / "doctor.sh"
PLUGIN_ID = "agentstack-codex-app@agentstack-local"


def _fake_commands(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    codex = fake_bin / "codex"
    codex.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "if os.environ.get('AGENTSTACK_TEST_CODEX_LOG'):\n"
        "    with open(os.environ['AGENTSTACK_TEST_CODEX_LOG'], 'a') as log:\n"
        "        log.write(json.dumps({'args': sys.argv[1:], 'home': os.environ.get('CODEX_HOME')}) + '\\n')\n"
        "if sys.argv[1:] == ['plugin', 'list', '--json']:\n"
        "    time.sleep(float(os.environ.get('AGENTSTACK_TEST_CODEX_DELAY', '0')))\n"
        "    print(os.environ['AGENTSTACK_TEST_PLUGIN_LIST'])\n"
        "    raise SystemExit(int(os.environ.get('AGENTSTACK_TEST_CODEX_RC', '0')))\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    tmux = fake_bin / "tmux"
    tmux.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    tmux.chmod(0o755)
    return fake_bin, codex


def _run_doctor(tmp_path: pathlib.Path, plugin_list: dict) -> tuple[subprocess.CompletedProcess, pathlib.Path, pathlib.Path]:
    home = tmp_path / "home"
    install_dir = home / ".agentstack"
    codex_home = home / ".codex"
    project = home / "project"
    for directory in (install_dir, codex_home, project):
        directory.mkdir(parents=True, exist_ok=True)
    mail_db = install_dir / "mail.sqlite3"
    mail_db.touch()
    fake_bin, codex = _fake_commands(tmp_path)
    exports = {
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_MAIL_DB": str(mail_db),
        "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled",
        "AGENTSTACK_MCP_URL": "http://127.0.0.1:9/mcp",
        "AGENTSTACK_PROJECT_KEY": str(project),
        "AGENTSTACK_CODEX_BIN": str(codex),
    }
    (install_dir / "env.sh").write_text(
        "".join(f"export {key}={shlex.quote(value)}\n" for key, value in exports.items()),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "CODEX_HOME": str(codex_home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "AGENTSTACK_TEST_PLUGIN_LIST": json.dumps(plugin_list),
        }
    )
    result = subprocess.run(
        ["/bin/bash", str(DOCTOR), "--install-dir", str(install_dir)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, codex, codex_home


@pytest.mark.parametrize(
    ("plugin_list", "state_text"),
    [
        ({"installed": []}, "is not installed"),
        ({"installed": [{"pluginId": PLUGIN_ID, "enabled": False}]}, "installed but disabled"),
        ({"installed": [{"pluginId": PLUGIN_ID, "enabled": True}]}, "installed and enabled"),
    ],
)
def test_doctor_reports_launcher_paths_and_plugin_state(tmp_path, plugin_list, state_text):
    result, codex, codex_home = _run_doctor(tmp_path, plugin_list)
    output = result.stdout + result.stderr

    assert f"ok: Codex launcher binary {codex}" in output
    assert f"ok: Codex launcher CODEX_HOME {codex_home}" in output
    assert state_text in output
    if state_text == "installed and enabled":
        assert "Codex lifecycle hook approval is unknown" in output
        assert "/hooks" in output
    else:
        assert "Claude-only use is unaffected" in output
        assert "warn: Codex history binding plugin" not in output
        assert "missing: Codex history binding plugin" not in output


# Exercise just the read-only diagnostic without unrelated missing-install
# errors obscuring whether this optional feature changes the doctor status.
def _probe(tmp_path, payload, *, returncode=0, delay=0, missing_binary=False, missing_home=False):
    fake_bin, codex = _fake_commands(tmp_path)
    codex_home = tmp_path / "custom codex home"
    if not missing_home:
        codex_home.mkdir()
        (codex_home / "config.toml").write_text("# user-owned; do not modify\n")
    log = tmp_path / "codex-calls.jsonl"
    source = DOCTOR.read_text()
    functions = source[source.index("codex_launcher_search_path() {"):source.index('\nCODEX_HOME="')]
    script = (
        "set -euo pipefail\n"
        + f"PYTHON_BIN={shlex.quote(sys.executable)}\n"
        + "status=0\n"
        + functions
        + '\nreport_codex_history_binding_prereqs "$1" "$2"\nexit "$status"\n'
    )
    env = dict(
        os.environ,
        AGENTSTACK_TEST_PLUGIN_LIST=payload if isinstance(payload, str) else json.dumps(payload),
        AGENTSTACK_TEST_CODEX_RC=str(returncode),
        AGENTSTACK_TEST_CODEX_DELAY=str(delay),
        AGENTSTACK_TEST_CODEX_LOG=str(log),
    )
    result = subprocess.run(
        ["/bin/bash", "-c", script, "doctor-probe", "" if missing_binary else str(codex), str(codex_home)],
        env=env,
        capture_output=True,
        text=True,
        timeout=12,
    )
    assert result.returncode == 0, result.stderr
    if not missing_home:
        assert (codex_home / "config.toml").read_text() == "# user-owned; do not modify\n"
    return result, log, codex_home


@pytest.mark.parametrize("payload", [
    "not json",
    {},
    [],
    {"installed": None},
    {"installed": [None]},
    {"installed": [{"id": PLUGIN_ID, "enabled": True}]},
    {"installed": [{"pluginId": PLUGIN_ID}]},
    {"installed": [{"pluginId": PLUGIN_ID, "enabled": "false"}]},
])
def test_unreadable_or_changed_registry_is_unknown_not_absent(tmp_path, payload):
    result, _, _ = _probe(tmp_path, payload)
    assert "plugin status is unknown" in result.stdout
    assert "is not installed" not in result.stdout
    assert "/hooks" in result.stdout


def test_failed_cli_cannot_claim_plugin_is_absent(tmp_path):
    result, _, _ = _probe(tmp_path, {"installed": []}, returncode=2)
    assert "plugin status is unknown" in result.stdout
    assert "is not installed" not in result.stdout


def test_stalled_cli_is_bounded_and_reports_unknown(tmp_path):
    result, _, _ = _probe(tmp_path, {"installed": []}, delay=10)
    assert "plugin status is unknown" in result.stdout


def test_probe_uses_requested_home_and_only_lists_plugins(tmp_path):
    result, log, codex_home = _probe(tmp_path, {"installed": [{"pluginId": PLUGIN_ID, "enabled": True}]})
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls == [{"args": ["plugin", "list", "--json"], "home": str(codex_home)}]
    assert "installed and enabled" in result.stdout
    assert "hook approval is unknown" in result.stdout
    assert "new Codex process" in result.stdout


@pytest.mark.parametrize("missing", ["binary", "home"])
def test_claude_only_or_missing_home_is_informational(tmp_path, missing):
    result, log, codex_home = _probe(
        tmp_path, {"installed": []}, missing_binary=missing == "binary", missing_home=missing == "home",
    )
    assert "Claude-only use is unaffected" in result.stdout
    assert not log.exists()
    if missing == "home":
        assert not codex_home.exists()


@pytest.mark.parametrize("explicit", [True, False])
def test_binary_resolution_matches_child_launcher(tmp_path, explicit):
    fake_bin, codex = _fake_commands(tmp_path)
    source = DOCTOR.read_text()
    functions = source[source.index("codex_launcher_search_path() {"):source.index("report_codex_history_binding_prereqs() {")]
    # An invalid configured path falls back to PATH, just as find_codex_bin does.
    configured = str(codex) if explicit else str(tmp_path / "removed-codex")
    result = subprocess.run(
        ["/bin/bash", "-c", "set -euo pipefail\n" + functions + "\nresolve_launcher_codex_bin"],
        env=dict(os.environ, AGENTSTACK_CODEX_BIN=configured, PATH=f"{fake_bin}:{os.environ['PATH']}"),
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == str(codex)
