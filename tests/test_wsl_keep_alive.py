"""Closing every Ubuntu window must not stop the agents running in WSL.

WSL stops a distro about 15 s after its last Windows-side client exits, with
tmux, the dashboard and every agent inside it (seen on WSL 2.7.13, 2026-09-29:
13-16 s after the last wsl.exe ended, with only `[wsl2] vmIdleTimeout=-1`
set). `[general] instanceIdleTimeout=-1` in the Windows %USERPROFILE%\\.wslconfig
kept it running. The installer adds it by default and doctor reports it.

The installer and doctor blocks are extracted and run with the WSL check
stubbed and fake `cmd.exe` / `wslpath` on PATH, so neither the real HOME nor a
real Windows .wslconfig is involved.
"""

from __future__ import annotations

import os
import pathlib
import shlex
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "lib" / "wslconfig.py"
INSTALL = ROOT / "scripts" / "install.sh"
DOCTOR = ROOT / "scripts" / "doctor.sh"
START = "# --- WSL keep-alive"
END = "# --- end WSL keep-alive ---"


def _helper(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True, timeout=30)


def _ensure(tmp_path, content: bytes | None, *extra: str):
    config = tmp_path / "win" / ".wslconfig"
    config.parent.mkdir(parents=True, exist_ok=True)
    if content is not None:
        config.write_bytes(content)
    backups = tmp_path / "backups"
    return config, backups, _helper("ensure", str(config), "--backup-dir", str(backups), *extra)


# --- the .wslconfig helper ----------------------------------------------------

def test_creates_the_file_with_crlf_and_no_bom(tmp_path):
    config, backups, result = _ensure(tmp_path, None)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "created"
    assert config.read_bytes() == b"[general]\r\ninstanceIdleTimeout=-1\r\n"
    assert not backups.exists()


def test_adds_a_general_section_and_keeps_everything_else(tmp_path):
    original = b"# mine\r\n[wsl2]\r\nvmIdleTimeout=-1\r\nmemory=8GB\r\n\r\n"
    config, backups, result = _ensure(tmp_path, original)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("added ")
    assert config.read_bytes() == (
        b"# mine\r\n[wsl2]\r\nvmIdleTimeout=-1\r\nmemory=8GB\r\n\r\n[general]\r\ninstanceIdleTimeout=-1\r\n"
    )
    backup = pathlib.Path(result.stdout.split(" ", 1)[1].strip())
    assert backup.parent.parent == backups
    assert backup.read_bytes() == original


def test_adds_the_key_inside_an_existing_general_section(tmp_path):
    original = b"[general]\r\nguiApplications=false\r\n\r\n[wsl2]\r\nvmIdleTimeout=-1\r\n"
    config, _, result = _ensure(tmp_path, original)
    assert result.returncode == 0, result.stderr
    assert config.read_bytes() == (
        b"[general]\r\nguiApplications=false\r\ninstanceIdleTimeout=-1\r\n\r\n[wsl2]\r\nvmIdleTimeout=-1\r\n"
    )


def test_lf_and_bom_input_is_written_back_as_crlf_without_bom(tmp_path):
    config, _, result = _ensure(tmp_path, b"\xef\xbb\xbf[wsl2]\nvmIdleTimeout=-1\n")
    assert result.returncode == 0, result.stderr
    assert config.read_bytes() == b"[wsl2]\r\nvmIdleTimeout=-1\r\n\r\n[general]\r\ninstanceIdleTimeout=-1\r\n"


def test_an_existing_value_is_reported_and_never_changed(tmp_path):
    for value in (b"-1", b"60000"):
        original = b"[General]\r\ninstanceidletimeout = " + value + b"\r\n"
        config, backups, result = _ensure(tmp_path, original)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == f"present {value.decode()}"
        assert config.read_bytes() == original
        assert not backups.exists()


def test_a_key_in_another_section_does_not_count(tmp_path):
    config, _, result = _ensure(tmp_path, b"[wsl2]\r\ninstanceIdleTimeout=-1\r\n")
    assert result.stdout.startswith("added ")
    assert config.read_bytes().endswith(b"[general]\r\ninstanceIdleTimeout=-1\r\n")


def test_dry_run_writes_nothing(tmp_path):
    config, backups, result = _ensure(tmp_path, None, "--dry-run")
    assert result.stdout.strip() == "would-create"
    assert not config.exists()
    config, backups, result = _ensure(tmp_path, b"[wsl2]\r\n", "--dry-run")
    assert result.stdout.strip() == "would-add"
    assert config.read_bytes() == b"[wsl2]\r\n"
    assert not backups.exists()


def test_a_file_that_is_not_utf8_is_left_alone(tmp_path):
    original = "[wsl2]\r\n".encode("utf-16")
    config, backups, result = _ensure(tmp_path, original)
    assert result.returncode == 3
    assert "left unchanged" in result.stderr
    assert config.read_bytes() == original
    assert not backups.exists()


def test_read_classifies_the_value(tmp_path):
    config = tmp_path / ".wslconfig"
    assert _helper("read", str(config)).stdout.strip() == "unset"
    for text, expected in (
        ("[wsl2]\nvmIdleTimeout=-1\n", "unset"),
        ("[general]\ninstanceIdleTimeout=-1 # keep\n", "ok -1"),
        ("[general]\ninstanceIdleTimeout=15000\n", "other 15000"),
    ):
        config.write_text(text, encoding="utf-8")
        assert _helper("read", str(config)).stdout.strip() == expected


# --- installer and doctor -----------------------------------------------------

def _fake_windows(tmp_path, *, with_interop=True) -> tuple[pathlib.Path, pathlib.Path]:
    """Fake cmd.exe / wslpath mapping C:\\Users\\tester to a temporary folder."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    profile = tmp_path / "c" / "Users" / "tester"
    profile.mkdir(parents=True, exist_ok=True)
    if with_interop:
        (bin_dir / "cmd.exe").write_text(
            "#!/bin/sh\n"
            'echo "\'\\\\wsl.localhost\\Ubuntu\' CMD.EXE was started with the above path" >&2\n'
            "printf 'C:\\\\Users\\\\tester\\r\\n'\n",
            encoding="utf-8",
        )
        (bin_dir / "wslpath").write_text(
            "#!/bin/sh\n"
            '[ "$1" = -u ] && [ "$2" = "C:\\Users\\tester" ] || exit 1\n'
            f"echo {shlex.quote(str(profile))}\n",
            encoding="utf-8",
        )
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    return bin_dir, profile / ".wslconfig"


def _block(path: pathlib.Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index(START)
    return text[start:text.index(END, start) + len(END)]


def _install(tmp_path, *, wsl=True, setting="1", dry_run=False, interop=True):
    bin_dir, config = _fake_windows(tmp_path, with_interop=interop)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    script = (
        "set -euo pipefail\n"
        f"SCRIPT_DIR={shlex.quote(str(ROOT / 'scripts'))}\n"
        f"PYTHON_BIN={shlex.quote(sys.executable)}\n"
        f"BACKUPS_DIR={shlex.quote(str(tmp_path / 'backups'))}\n"
        f"DRY_RUN={'true' if dry_run else 'false'}\n"
        f"WSL_KEEP_ALIVE_SETTING={setting}\n"
        "say() { printf '%s\\n' \"$*\"; }\n"
        "warn() { printf 'warning: %s\\n' \"$*\" >&2; }\n"
        "plan() { printf 'plan: %s\\n' \"$*\"; }\n"
        + ("running_under_wsl() { return 0; }\n" if wsl else "running_under_wsl() { return 1; }\n")
        + _block(INSTALL)
        + '\nensure_wsl_keep_alive\nprintf "NOTE=%s\\n" "$WSL_KEEP_ALIVE_NOTE"\n'
    )
    env = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin"}
    result = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)
    return result, config


def test_install_adds_the_setting_and_says_a_wsl_restart_is_needed(tmp_path):
    result, config = _install(tmp_path)
    assert result.returncode == 0, result.stderr
    assert config.read_bytes() == b"[general]\r\ninstanceIdleTimeout=-1\r\n"
    assert f"created {config}" in result.stdout
    note = [line for line in result.stdout.splitlines() if line.startswith("NOTE=")][0]
    assert "next WSL start" in note and "wsl --shutdown" in note
    assert "does not" in note  # the installer never shuts WSL down itself


def test_install_keeps_an_existing_value_and_warns_when_it_still_stops(tmp_path):
    _, config = _fake_windows(tmp_path)
    config.write_bytes(b"[general]\r\ninstanceIdleTimeout=60000\r\n")
    result, config = _install(tmp_path)
    assert result.returncode == 0, result.stderr
    assert config.read_bytes() == b"[general]\r\ninstanceIdleTimeout=60000\r\n"
    assert "already has instanceIdleTimeout=60000; left as is" in result.stderr


def test_install_opt_out_and_non_wsl_leave_the_file_alone(tmp_path):
    result, config = _install(tmp_path, setting="0")
    assert result.returncode == 0, result.stderr
    assert "WSL keep-alive: off" in result.stdout
    assert not config.exists()
    result, config = _install(tmp_path, wsl=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "NOTE="
    assert not config.exists()


def test_install_dry_run_only_plans(tmp_path):
    result, config = _install(tmp_path, dry_run=True)
    assert result.returncode == 0, result.stderr
    assert f"plan: create {config} with [general] instanceIdleTimeout=-1" in result.stdout
    assert not config.exists()


def test_install_without_windows_interop_explains_the_manual_fix(tmp_path):
    result, config = _install(tmp_path, interop=False)
    assert result.returncode == 0, result.stderr
    assert "could not find the Windows user folder" in result.stderr
    assert "instanceIdleTimeout=-1" in result.stderr and "wsl --shutdown" in result.stderr
    assert not config.exists()


def test_install_persists_the_setting_and_rejects_other_values(tmp_path):
    source = INSTALL.read_text(encoding="utf-8")
    env_block = source[source.index("write_env_file() {"):source.index("stop_new_agent_mail() {")]
    assert '"AGENTSTACK_WSL_KEEP_ALIVE": "$WSL_KEEP_ALIVE_SETTING"' in env_block
    assert "ensure_wsl_keep_alive\n" in source[source.index("main() {"):]
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        ["/bin/bash", str(INSTALL), "--dry-run", "--install-dir", str(tmp_path / "stack"),
         "--project-key", str(tmp_path)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "AGENTSTACK_WSL_KEEP_ALIVE": "yes"},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert "AGENTSTACK_WSL_KEEP_ALIVE must be 0 or 1" in result.stderr
    assert not (tmp_path / "stack").exists()


def _doctor(tmp_path, *, content: bytes | None, setting: str | None = None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    bin_dir, config = _fake_windows(tmp_path)
    if content is not None:
        config.write_bytes(content)
    script = (
        "set -euo pipefail\n"
        f"SCRIPT_DIR={shlex.quote(str(ROOT / 'scripts'))}\n"
        f"PYTHON_BIN={shlex.quote(sys.executable)}\n"
        "running_under_wsl() { return 0; }\n"
        + _block(DOCTOR)
        + "\nreport_wsl_keep_alive\n"
    )
    env = {"HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin"}
    if setting is not None:
        env["AGENTSTACK_WSL_KEEP_ALIVE"] = setting
    return subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)


def test_doctor_warns_with_the_fix_when_unset(tmp_path):
    result = _doctor(tmp_path, content=b"[wsl2]\r\nvmIdleTimeout=-1\r\n")
    assert result.returncode == 0, result.stderr
    assert "warn: WSL keep-alive: WSL stops the dashboard and agents about 15 s after the last Ubuntu window closes" in result.stdout
    assert "instanceIdleTimeout=-1" in result.stdout and "wsl --shutdown" in result.stdout


def test_doctor_ok_when_set_and_note_when_opted_out(tmp_path):
    result = _doctor(tmp_path / "set", content=b"[general]\r\ninstanceIdleTimeout=-1\r\n")
    assert "ok: WSL keep-alive" in result.stdout
    assert "warn:" not in result.stdout
    result = _doctor(tmp_path / "other", content=b"[general]\r\ninstanceIdleTimeout=15000\r\n")
    assert "warn: WSL keep-alive: WSL stops the dashboard and agents 15000 ms" in result.stdout
    result = _doctor(tmp_path / "off", content=None, setting="0")
    assert "note: WSL keep-alive is off" in result.stdout
    assert "warn:" not in result.stdout


def test_doctor_is_silent_outside_wsl(tmp_path):
    text = DOCTOR.read_text(encoding="utf-8")
    assert "report_wsl_keep_alive() {\n  running_under_wsl || return 0\n" in text
    assert os.path.exists(HELPER)
