"""The installer must record a `codex` that can actually run.

Under WSL, PATH also carries the Windows PATH. On 2026-09-28 the installer
picked /mnt/c/Users/<user>/AppData/Roaming/npm/codex -- the Windows npm shim --
and saved it as AGENTSTACK_CODEX_BIN; run by the Linux node it died with
"Missing optional dependency @openai/codex-linux-x64", so every Codex child the
dashboard spawned ended before its prompt arrived.

The resolution block of scripts/install.sh is extracted and run with the WSL
check and the Windows mount root stubbed, since /mnt/c cannot exist on macOS CI.
"""

from __future__ import annotations

import os
import pathlib
import shlex
import subprocess
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALL = ROOT / "scripts" / "install.sh"


def _block() -> str:
    text = INSTALL.read_text(encoding="utf-8")
    start = text.index("# --- codex launcher resolution")
    end = text.index("# A Node-installed `codex` is a wrapper", start)
    return text[start:end]


def _script(path: pathlib.Path, body: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


WORKS = 'echo "codex-cli 0.157.0"\n'
BROKEN = 'echo "Error: Missing optional dependency @openai/codex-linux-x64" >&2\nexit 1\n'


def _resolve(tmp_path, *, path_dirs, wsl=False, explicit="", installed="", timeout=5):
    """Run the block; returns (exit code, resolved CODEX_BIN_SETTING, stderr).

    The default probe budget is generous so a working fake codex is not failed
    by a loaded machine; the hang tests pass a short timeout explicitly.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    text = INSTALL.read_text(encoding="utf-8")
    stubs = (
        "set -euo pipefail\n"
        f". {shlex.quote(str(INSTALL.parents[1] / 'hooks' / 'project-context.sh'))}\n"
        f"INSTALL_DIR={shlex.quote(str(tmp_path / 'install'))}\n"
        "RESET_SETTINGS=0\n"
        # An explicit value is what --codex-bin / AGENTSTACK_CODEX_BIN supplies.
        f"OPTION_GIVEN={'AGENTSTACK_CODEX_BIN' if explicit else ''}\n"
        f"CODEX_BIN_SETTING={shlex.quote(explicit)}\n"
        f"agentstack_installed_env_value() {{ printf '%s\\n' {shlex.quote(installed)}; }}\n"
        + text[text.index("# --- setting resolution"):text.index("# --- end setting resolution ---")]
    )
    block = _block()
    # Stub the environment checks after their definitions inside the block.
    overrides = (
        ("running_under_wsl() { return 0; }\n" if wsl else "running_under_wsl() { return 1; }\n")
        + f"WSL_WINDOWS_MOUNT_ROOT={shlex.quote(str(tmp_path / 'mnt'))}\n"
        + f"CODEX_VERSION_TIMEOUT_SECONDS={timeout}\n"
    )
    marker = "# --- end codex launcher resolution ---\n"
    block = block.replace(marker, marker + overrides, 1)
    script = stubs + block + '\nprintf "RESOLVED=%s\\n" "$CODEX_BIN_SETTING"\n'
    env = {
        "HOME": str(home),
        "PATH": ":".join(str(d) for d in path_dirs) + ":/usr/bin:/bin",
    }
    result = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)
    resolved = ""
    for line in result.stdout.splitlines():
        if line.startswith("RESOLVED="):
            resolved = line[len("RESOLVED="):]
    return result.returncode, resolved, result.stderr


def _windows_codex(tmp_path, body=WORKS):
    return _script(tmp_path / "mnt" / "c" / "Users" / "someone" / "AppData" / "Roaming" / "npm" / "codex", body)


def test_wsl_skips_the_windows_codex_and_takes_the_next_on_path(tmp_path):
    windows = _windows_codex(tmp_path)  # even one that would answer --version
    linux = _script(tmp_path / "linux-bin" / "codex", WORKS)
    code, resolved, _ = _resolve(tmp_path, path_dirs=[windows.parent, linux.parent], wsl=True)
    assert code == 0
    assert resolved == str(linux)


def test_outside_wsl_the_same_path_is_an_ordinary_candidate(tmp_path):
    windows = _windows_codex(tmp_path)
    linux = _script(tmp_path / "linux-bin" / "codex", WORKS)
    _, resolved, _ = _resolve(tmp_path, path_dirs=[windows.parent, linux.parent], wsl=False)
    assert resolved == str(windows)


def test_a_codex_that_fails_version_is_skipped(tmp_path):
    broken = _script(tmp_path / "broken-bin" / "codex", BROKEN)
    linux = _script(tmp_path / "linux-bin" / "codex", WORKS)
    _, resolved, _ = _resolve(tmp_path, path_dirs=[broken.parent, linux.parent])
    assert resolved == str(linux)


def test_a_codex_that_hangs_on_version_is_skipped_within_the_timeout(tmp_path):
    hung = _script(tmp_path / "hung-bin" / "codex", "sleep 30\n")
    linux = _script(tmp_path / "linux-bin" / "codex", WORKS)
    _, resolved, _ = _resolve(tmp_path, path_dirs=[hung.parent, linux.parent], timeout=1)
    assert resolved == str(linux)


def test_the_per_user_npm_prefix_is_found_when_not_on_path(tmp_path):
    # WSL docs install Codex under ~/.npm-global/bin, which a fresh shell may lack.
    windows = _windows_codex(tmp_path)
    npm_global = _script(tmp_path / "home" / ".npm-global" / "bin" / "codex", WORKS)
    _, resolved, _ = _resolve(tmp_path, path_dirs=[windows.parent], wsl=True)
    assert resolved == str(npm_global)


def test_nothing_usable_leaves_the_setting_empty(tmp_path):
    windows = _windows_codex(tmp_path)
    code, resolved, _ = _resolve(tmp_path, path_dirs=[windows.parent], wsl=True)
    assert code == 0 and resolved == ""


def test_installed_windows_codex_is_stale_and_resolved_again(tmp_path):
    windows = _windows_codex(tmp_path)
    linux = _script(tmp_path / "linux-bin" / "codex", WORKS)
    code, resolved, stderr = _resolve(
        tmp_path, path_dirs=[windows.parent, linux.parent], wsl=True, installed=str(windows))
    assert code == 0
    assert resolved == str(linux)
    assert f"installed AGENTSTACK_CODEX_BIN={windows} is stale" in stderr
    assert "Windows install under" in stderr


def test_installed_codex_that_fails_version_is_stale(tmp_path):
    broken = _script(tmp_path / "broken-bin" / "codex", BROKEN)
    linux = _script(tmp_path / "linux-bin" / "codex", WORKS)
    _, resolved, stderr = _resolve(tmp_path, path_dirs=[linux.parent], installed=str(broken))
    assert resolved == str(linux)
    # It ended at once with an error: say so, not that it ran out of time (#121).
    assert "--version' exited with status 1 after 0." in stderr
    assert "Missing optional dependency @openai/codex-linux-x64" in stderr
    assert "within" not in stderr


def test_installed_working_codex_is_kept(tmp_path):
    kept = _script(tmp_path / "kept-bin" / "codex", WORKS)
    other = _script(tmp_path / "linux-bin" / "codex", WORKS)
    _, resolved, stderr = _resolve(tmp_path, path_dirs=[other.parent], installed=str(kept))
    assert resolved == str(kept)
    assert "stale" not in stderr


def test_explicit_codex_that_cannot_run_stops_with_the_reason(tmp_path):
    broken = _script(tmp_path / "broken-bin" / "codex", BROKEN)
    code, _, stderr = _resolve(tmp_path, path_dirs=[], explicit=str(broken))
    assert code == 2
    assert f"cannot be used: {broken}" in stderr
    assert "--version' exited with status 1" in stderr
    assert "Missing optional dependency @openai/codex-linux-x64" in stderr


def test_explicit_windows_codex_under_wsl_stops_with_the_reason(tmp_path):
    windows = _windows_codex(tmp_path)
    code, _, stderr = _resolve(tmp_path, path_dirs=[], wsl=True, explicit=str(windows))
    assert code == 2
    assert "Windows install under" in stderr


def test_explicit_working_codex_is_used_as_is(tmp_path):
    explicit = _script(tmp_path / "explicit-bin" / "codex", WORKS)
    code, resolved, _ = _resolve(tmp_path, path_dirs=[], explicit=str(explicit))
    assert code == 0 and resolved == str(explicit)


def test_resolution_block_is_bash_3_2_clean():
    result = subprocess.run(["/bin/bash", "-n", str(INSTALL)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_a_codex_that_ignores_term_is_still_bounded(tmp_path):
    # TERM is ignored and a child keeps running: the probe must still return.
    stubborn = tmp_path / "stubborn-bin" / "codex"
    stubborn.parent.mkdir(parents=True)
    stubborn.write_text('#!/bin/bash\ntrap "" TERM\nwhile :; do sleep 0.1; done\n', encoding="utf-8")
    stubborn.chmod(0o755)
    linux = _script(tmp_path / "linux-bin" / "codex", WORKS)
    started = time.monotonic()
    _, resolved, _ = _resolve(tmp_path, path_dirs=[stubborn.parent, linux.parent], timeout=1)
    assert resolved == str(linux)
    assert time.monotonic() - started < 8


def _wrapper_with_stubborn_native(tmp_path) -> tuple[pathlib.Path, pathlib.Path]:
    """An npm-style wrapper that exits on TERM over a native child that ignores it."""
    pidfile = tmp_path / "native.pid"
    native = tmp_path / "native"
    native.write_text('#!/bin/bash\ntrap "" TERM\nwhile :; do sleep 0.1; done\n', encoding="utf-8")
    native.chmod(0o755)
    wrapper = tmp_path / "wrapper-bin" / "codex"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        f"#!/bin/bash\n{shlex.quote(str(native))} &\necho $! > {shlex.quote(str(pidfile))}\nwait\n",
        encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper, pidfile


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_a_native_child_that_ignores_term_is_not_left_behind(tmp_path):
    wrapper, pidfile = _wrapper_with_stubborn_native(tmp_path)
    linux = _script(tmp_path / "linux-bin" / "codex", WORKS)
    try:
        _, resolved, _ = _resolve(tmp_path, path_dirs=[wrapper.parent, linux.parent], timeout=1)
        assert resolved == str(linux)
        native_pid = int(pidfile.read_text())
        deadline = time.monotonic() + 3
        while _alive(native_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(native_pid), "native child survived the probe"
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), 9)
            except (ProcessLookupError, ValueError):
                pass


def _stop_functions(script: str) -> str:
    text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
    parts = []
    for name in ("codex_probe_stop", "codex_process_start"):
        start = text.index(f"\n{name}() {{") + 1
        parts.append(text[start:text.index("\n}\n", start) + 3])
    return "\n".join(parts)


# Signals are stubbed: 410000 is the probe, 410001 its child. No real process
# is signalled.
_SIGNAL_STUBS = r"""
tick=0
pgrep() { [[ "$2" == 410000 ]] && echo 410001; return 0; }
sleep() { tick=$((tick + 1)); }
wait() { return 0; }
ps() { printf '%s\n' "$(start_of "$4")"; }
kill() {
  case "$1:$2" in
    -TERM:*) echo "TERM $2" ;;
    -KILL:*) echo "KILL $2" ;;
    -0:410000) return 0 ;;
    -0:410001) child_alive ;;
  esac
}
"""


@pytest.mark.parametrize("script", ["install.sh", "doctor.sh"])
def test_a_pid_seen_gone_is_never_killed_even_if_reused(script):
    # The child exits during the grace period; its PID then shows up alive again
    # (reused by an unrelated process). It must not receive KILL.
    body = (_stop_functions(script) + _SIGNAL_STUBS
            + 'start_of() { echo "Tue Sep 29 01:00:00 2026"; }\n'
            + 'child_alive() { [[ "$tick" -ge 2 ]]; }\n'
            + "codex_probe_stop 410000\n")
    out = subprocess.run(["/bin/bash", "-c", body], capture_output=True, text=True, timeout=10).stdout.split("\n")
    assert "TERM 410001" in out
    assert "KILL 410001" not in out
    assert "KILL 410000" in out


@pytest.mark.parametrize("script", ["install.sh", "doctor.sh"])
def test_a_pid_with_a_different_start_time_is_not_killed(script):
    # Always alive, but by KILL time the PID belongs to a process that started later.
    body = (_stop_functions(script) + _SIGNAL_STUBS
            + 'start_of() { if [[ "$1" == 410001 && "$tick" -ge 5 ]]; then echo "Tue Sep 29 01:00:09 2026"; '
              'else echo "Tue Sep 29 01:00:00 2026"; fi; }\n'
            + "child_alive() { return 0; }\n"
            + "codex_probe_stop 410000\n")
    out = subprocess.run(["/bin/bash", "-c", body], capture_output=True, text=True, timeout=10).stdout.split("\n")
    assert "KILL 410001" not in out
    assert "KILL 410000" in out


@pytest.mark.parametrize("script", ["install.sh", "doctor.sh"])
def test_a_stubborn_child_with_the_same_start_time_is_killed(script):
    body = (_stop_functions(script) + _SIGNAL_STUBS
            + 'start_of() { echo "Tue Sep 29 01:00:00 2026"; }\n'
            + "child_alive() { return 0; }\n"
            + "codex_probe_stop 410000\n")
    out = subprocess.run(["/bin/bash", "-c", body], capture_output=True, text=True, timeout=10).stdout.split("\n")
    assert "KILL 410001" in out


def _run_stop(script, extra):
    body = _stop_functions(script) + _SIGNAL_STUBS + extra + "codex_probe_stop 410000\necho RETURNED\n"
    return subprocess.run(["/bin/bash", "-c", body], capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize("script", ["install.sh", "doctor.sh"])
def test_probe_stop_returns_even_if_nothing_exits(script):
    # Probe and child survive TERM and KILL (stubbed): the function still returns,
    # never reaching an unbounded wait, and says what it left.
    result = _run_stop(script, 'start_of() { echo "Tue Sep 29 01:00:00 2026"; }\n'
                               "child_alive() { return 0; }\n"
                               'wait() { echo "WAIT $1"; }\n')
    out = result.stdout.split("\n")
    assert "RETURNED" in out
    assert "WAIT 410000" not in out
    assert "did not exit after KILL; leaving it" in result.stderr


@pytest.mark.parametrize("script", ["install.sh", "doctor.sh"])
def test_probe_is_killed_even_when_ps_stops_answering(script):
    # First ps answers, the re-read before KILL fails: the probe (our own child)
    # is still KILLed, the unverifiable descendant is left with a note.
    result = _run_stop(script, 'start_of() { if [[ "$tick" -lt 3 ]]; then echo "Tue Sep 29 01:00:00 2026"; fi; }\n'
                               "child_alive() { return 0; }\n")
    out = result.stdout.split("\n")
    assert "KILL 410000" in out
    assert "KILL 410001" not in out
    assert "RETURNED" in out
    assert "left process 410001" in result.stderr


def test_an_explicit_codex_that_hangs_says_it_ran_out_of_time(tmp_path):
    hung = _script(tmp_path / "hung-bin" / "codex", "sleep 30\n")
    code, _, stderr = _resolve(tmp_path, path_dirs=[], explicit=str(hung), timeout=1)
    assert code == 2
    assert "--version' did not finish within 1s and was stopped" in stderr
    assert "exited with status" not in stderr


def test_an_explicit_codex_that_fails_silently_says_there_was_no_output(tmp_path):
    silent = _script(tmp_path / "silent-bin" / "codex", "exit 7\n")
    code, _, stderr = _resolve(tmp_path, path_dirs=[], explicit=str(silent))
    assert code == 2
    assert "--version' exited with status 7 after 0." in stderr
    assert "(no error output)" in stderr
