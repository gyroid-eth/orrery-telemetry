"""The Codex App integration installer must pick a codex that can run.

On WSL (2026-09-29) it took `command -v codex`, the Windows npm shim under
/mnt/c first on PATH, and failed with "Missing optional dependency
@openai/codex-linux-x64", although the core installer had saved a working
codex in ~/.agentstack/env.sh. It now uses the core rules (hooks/codex-bin.sh)
and the saved value. Outside macOS it installs without the launchd service
instead of stopping.

The resolution block is extracted and run with the WSL check and the Windows
mount root stubbed, since /mnt/c cannot exist on macOS CI.
"""

from __future__ import annotations

import pathlib
import shlex
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install-codex-app-integration.sh"


def _block() -> str:
    text = INSTALLER.read_text(encoding="utf-8")
    start = text.index("# Codex candidate rules shared with the core installer")
    return text[start:text.index("\nvalidate() {", start)]


def _codex(path: pathlib.Path, works: bool = True) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = 'echo "codex-cli 0.158.0"\n' if works else \
        'echo "Error: Missing optional dependency @openai/codex-linux-x64" >&2\nexit 1\n'
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _resolve(tmp_path, *, path_dirs, explicit="", saved="", env_value="", wsl=True, uname="Linux"):
    home = tmp_path / "home"
    (home / ".agentstack").mkdir(parents=True, exist_ok=True)
    if saved:
        (home / ".agentstack" / "env.sh").write_text(
            f"export AGENTSTACK_CODEX_BIN={shlex.quote(saved)}\n", encoding="utf-8")
    script = (
        "set -euo pipefail\n"
        f"REPO_ROOT={shlex.quote(str(ROOT))}\n"
        f"CODEX_BIN={shlex.quote(explicit)}\n"
        "NO_SERVICE=false\n"
        "say() { printf '%s\\n' \"$*\"; }\n"
        "warn() { printf 'warning: %s\\n' \"$*\" >&2; }\n"
        "die() { printf 'error: %s\\n' \"$*\" >&2; exit 1; }\n"
        f"uname() {{ echo {uname}; }}\n"
        + _block()
        + ("\nrunning_under_wsl() { return 0; }\n" if wsl else "\nrunning_under_wsl() { return 1; }\n")
        + f"WSL_WINDOWS_MOUNT_ROOT={shlex.quote(str(tmp_path / 'mnt'))}\n"
        + 'resolve_codex_bin\ndefault_service_mode\nprintf "CODEX=%s NO_SERVICE=%s\\n" "$CODEX_BIN" "$NO_SERVICE"\n'
    )
    env = {"HOME": str(home), "PATH": ":".join(map(str, path_dirs)) + ":/usr/bin:/bin"}
    if env_value:
        env["AGENTSTACK_CODEX_BIN"] = env_value
    return subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)


def _value(result, key):
    for part in result.stdout.split():
        if part.startswith(key + "="):
            return part[len(key) + 1:]
    return None


def test_the_saved_codex_wins_over_the_windows_shim_on_path(tmp_path):
    windows = _codex(tmp_path / "mnt" / "c" / "Users" / "someone" / "AppData" / "Roaming" / "npm" / "codex")
    good = _codex(tmp_path / "home" / ".npm-global" / "bin" / "codex")
    result = _resolve(tmp_path, path_dirs=[windows.parent], saved=str(good))
    assert result.returncode == 0, result.stderr
    assert _value(result, "CODEX") == str(good)


def test_without_a_saved_value_the_windows_shim_is_skipped_with_its_reason(tmp_path):
    windows = _codex(tmp_path / "mnt" / "c" / "npm" / "codex")
    good = _codex(tmp_path / "home" / ".npm-global" / "bin" / "codex")
    result = _resolve(tmp_path, path_dirs=[windows.parent])
    assert _value(result, "CODEX") == str(good)
    assert f"skipping codex {windows}: it is a Windows install under" in result.stderr


def test_a_saved_value_that_cannot_run_is_skipped_and_said(tmp_path):
    broken = _codex(tmp_path / "old" / "codex", works=False)
    good = _codex(tmp_path / "bin" / "codex")
    result = _resolve(tmp_path, path_dirs=[good.parent], saved=str(broken))
    assert _value(result, "CODEX") == str(good)
    assert f"skipping AGENTSTACK_CODEX_BIN from env.sh ({broken})" in result.stderr


def test_the_environment_value_comes_before_the_saved_one(tmp_path):
    first = _codex(tmp_path / "a" / "codex")
    second = _codex(tmp_path / "b" / "codex")
    result = _resolve(tmp_path, path_dirs=[], env_value=str(first), saved=str(second))
    assert _value(result, "CODEX") == str(first)


def test_an_explicit_codex_that_cannot_run_stops_with_the_reason(tmp_path):
    windows = _codex(tmp_path / "mnt" / "c" / "npm" / "codex")
    good = _codex(tmp_path / "bin" / "codex")
    result = _resolve(tmp_path, path_dirs=[good.parent], explicit=str(windows))
    assert result.returncode != 0
    assert f"codex {windows} cannot be used: it is a Windows install under" in result.stderr


def test_nothing_usable_leaves_it_empty_for_validate_to_explain(tmp_path):
    windows = _codex(tmp_path / "mnt" / "c" / "npm" / "codex")
    result = _resolve(tmp_path, path_dirs=[windows.parent])
    assert result.returncode == 0 and _value(result, "CODEX") == ""
    text = INSTALLER.read_text(encoding="utf-8")
    assert "no usable codex found" in text


def test_outside_macos_the_service_is_off_by_default(tmp_path):
    good = _codex(tmp_path / "bin" / "codex")
    result = _resolve(tmp_path, path_dirs=[good.parent], uname="Linux")
    assert _value(result, "NO_SERVICE") == "true"
    assert "launchd is macOS-only" in result.stdout
    result = _resolve(tmp_path, path_dirs=[good.parent], uname="Darwin", wsl=False)
    assert _value(result, "NO_SERVICE") == "false"


def test_an_explicit_codex_that_does_not_answer_stops_before_anything_is_written(tmp_path):
    broken = _codex(tmp_path / "bin" / "codex", works=False)
    result = _resolve(tmp_path, path_dirs=[], explicit=str(broken), wsl=False)
    assert result.returncode != 0
    assert f"codex {broken} cannot be used: '{broken} --version' exited with status 1 after" in result.stderr
    assert "Missing optional dependency @openai/codex-linux-x64" in result.stderr


def test_the_hook_guidance_names_the_chosen_codex():
    text = INSTALLER.read_text(encoding="utf-8")
    guidance = text[text.index("say_hook_approval_guidance() {"):text.index("\n}\n", text.index("say_hook_approval_guidance() {"))]
    assert 'printf \'%q\' "$CODEX_BIN"' in guidance and "-C" in guidance
