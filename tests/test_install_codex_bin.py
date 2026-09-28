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


def _resolve(tmp_path, *, path_dirs, wsl=False, explicit="", installed="", timeout=2):
    """Run the block; returns (exit code, resolved CODEX_BIN_SETTING, stderr)."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    stubs = (
        "set -euo pipefail\n"
        f"INSTALL_DIR={shlex.quote(str(tmp_path / 'install'))}\n"
        f"CODEX_BIN_SETTING={shlex.quote(explicit)}\n"
        f"agentstack_installed_env_value() {{ printf '%s\\n' {shlex.quote(installed)}; }}\n"
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
    assert "--version' did not succeed" in stderr


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
    assert "--version' did not succeed" in stderr


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
