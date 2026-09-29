"""Which codex spawn_child.sh runs for a Codex child.

On WSL (2026-09-29) a Claude agent started from the dashboard ran /delegate for
a Codex child. Its shell had no AGENTSTACK_CODEX_BIN, and its PATH starts with
the Windows PATH, so the fallback `command -v codex` picked
/mnt/c/Users/<user>/AppData/Roaming/npm/codex. The Linux node cannot run that
npm shim ("Missing optional dependency @openai/codex-linux-x64") and the child
ended three seconds after spawn. The dashboard's own spawns worked because its
environment carries AGENTSTACK_CODEX_BIN.

find_codex_bin now tries AGENTSTACK_CODEX_BIN from the environment, then the
value the installer saved in env.sh (one line read, never sourced), then the
search path, and uses a candidate only if hooks/codex-bin.sh accepts it: not a
Windows install under /mnt on WSL, and answering --version.
"""
from __future__ import annotations

import pathlib
import re
import shlex
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPAWN = ROOT / "hooks" / "spawn_child.sh"
LIB = ROOT / "hooks" / "codex-bin.sh"
CONTEXT = ROOT / "hooks" / "project-context.sh"

WORKS = '#!/bin/sh\necho "codex-cli 0.158.0"\n'
BROKEN = 'echo "Error: Missing optional dependency @openai/codex-linux-x64" >&2\nexit 1\n'


def _extract(func: str) -> str:
    text = SPAWN.read_text(encoding="utf-8")
    start = text.index(f"\n{func}() {{") + 1
    return text[start:text.index("\n}\n", start) + 3]


def _script(path: pathlib.Path, body: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if body.startswith("#!") else "#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _resolve(tmp_path, *, path_dirs, env_bin=None, installed=None, wsl=False, lib=True):
    """Run resolve_codex_bin as spawn_child.sh defines it; returns (rc, stdout, stderr)."""
    home = tmp_path / "home"
    (home / ".agentstack").mkdir(parents=True, exist_ok=True)
    if installed is not None:
        (home / ".agentstack" / "env.sh").write_text(
            f"export AGENTSTACK_CODEX_BIN={shlex.quote(installed)}\n", encoding="utf-8")
    text = SPAWN.read_text(encoding="utf-8")
    start = text.index("# Codex candidate rules, and the env.sh reader")
    loader = text[start:text.index("codex_search_path() {", start)]
    script = (
        f"HOOKS_DIR={shlex.quote(str(ROOT / 'hooks') if lib else str(tmp_path / 'old-hooks'))}\n"
        + loader
        # Stub the platform after the library is loaded; /mnt cannot exist on macOS CI.
        + ("running_under_wsl() { return 0; }\n" if wsl else "running_under_wsl() { return 1; }\n")
        + f"WSL_WINDOWS_MOUNT_ROOT={shlex.quote(str(tmp_path / 'mnt'))}\n"
        + "CODEX_VERSION_TIMEOUT_SECONDS=5\n"
        + _extract("codex_search_path") + _extract("find_codex_bin") + _extract("resolve_codex_bin")
        + "resolve_codex_bin\n"
    )
    if not lib:
        (tmp_path / "old-hooks").mkdir(exist_ok=True)
        (tmp_path / "old-hooks" / "project-context.sh").write_text(CONTEXT.read_text(encoding="utf-8"))
    env = {"HOME": str(home), "PATH": ":".join(str(d) for d in path_dirs) + ":/usr/bin:/bin"}
    if env_bin is not None:
        env["AGENTSTACK_CODEX_BIN"] = env_bin
    result = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
    return result.returncode, result.stdout.strip(), result.stderr


def _windows_codex(tmp_path, body=WORKS):
    return _script(tmp_path / "mnt" / "c" / "Users" / "someone" / "AppData" / "Roaming" / "npm" / "codex", body)


def test_an_agent_shell_on_wsl_uses_the_installed_codex_not_the_windows_one(tmp_path):
    # The reported case: no AGENTSTACK_CODEX_BIN in the agent's shell, the
    # Windows npm dir first on PATH, and the installer's value in env.sh.
    windows = _windows_codex(tmp_path, BROKEN)
    linux = _script(tmp_path / "home" / ".npm-global" / "bin" / "codex", WORKS)
    rc, out, err = _resolve(tmp_path, path_dirs=[windows.parent], installed=str(linux), wsl=True)
    assert rc == 0, err
    assert out == str(linux)


def test_without_env_sh_the_search_skips_the_windows_codex_on_wsl(tmp_path):
    windows = _windows_codex(tmp_path)  # even one that would answer --version
    linux = _script(tmp_path / "linux-bin" / "codex", WORKS)
    rc, out, err = _resolve(tmp_path, path_dirs=[windows.parent, linux.parent], wsl=True)
    assert rc == 0, err
    assert out == str(linux)
    assert f"skipping codex {windows}: it is a Windows install under" in err


def test_the_search_skips_a_codex_that_fails_version(tmp_path):
    broken = _script(tmp_path / "broken-bin" / "codex", BROKEN)
    linux = _script(tmp_path / "linux-bin" / "codex", WORKS)
    rc, out, err = _resolve(tmp_path, path_dirs=[broken.parent, linux.parent])
    assert rc == 0, err
    assert out == str(linux)
    assert f"skipping codex {broken}: '{broken} --version' did not succeed" in err


def test_the_environment_value_wins_when_it_can_run(tmp_path):
    chosen = _script(tmp_path / "chosen" / "codex", WORKS)
    installed = _script(tmp_path / "installed" / "codex", WORKS)
    rc, out, _ = _resolve(tmp_path, path_dirs=[], env_bin=str(chosen), installed=str(installed))
    assert rc == 0 and out == str(chosen)


def test_a_value_that_cannot_run_is_skipped_with_its_source_and_reason(tmp_path):
    windows = _windows_codex(tmp_path)
    installed = _script(tmp_path / "installed" / "codex", WORKS)
    rc, out, err = _resolve(tmp_path, path_dirs=[], env_bin=str(windows), installed=str(installed), wsl=True)
    assert rc == 0 and out == str(installed)
    assert f"skipping AGENTSTACK_CODEX_BIN from environment ({windows})" in err
    # A stale env.sh value falls through to the search the same way.
    linux = _script(tmp_path / "linux-bin" / "codex", WORKS)
    rc, out, err = _resolve(tmp_path, path_dirs=[linux.parent], installed=str(windows), wsl=True)
    assert rc == 0 and out == str(linux)
    assert f"skipping AGENTSTACK_CODEX_BIN from env.sh ({windows})" in err


def test_nothing_usable_is_an_error_that_says_why(tmp_path):
    windows = _windows_codex(tmp_path)
    rc, out, err = _resolve(tmp_path, path_dirs=[windows.parent], wsl=True)
    assert rc == 1 and out == ""
    assert f"skipping codex {windows}" in err
    assert "no usable Codex CLI found" in err
    assert "a codex under /mnt is the Windows install" in err


def test_outside_wsl_a_path_under_mnt_is_an_ordinary_candidate(tmp_path):
    windows = _windows_codex(tmp_path)
    rc, out, _ = _resolve(tmp_path, path_dirs=[windows.parent], wsl=False)
    assert rc == 0 and out == str(windows)


def test_an_older_hooks_dir_without_the_library_keeps_the_previous_rules(tmp_path):
    first = _script(tmp_path / "first-bin" / "codex", BROKEN)
    rc, out, _ = _resolve(tmp_path, path_dirs=[first.parent], lib=False)
    assert rc == 0 and out == str(first)


def test_the_installed_env_sh_is_read_not_sourced(tmp_path):
    marker = tmp_path / "sourced"
    linux = _script(tmp_path / "linux-bin" / "codex", WORKS)
    home = tmp_path / "home" / ".agentstack"
    home.mkdir(parents=True)
    rc, out, _ = _resolve(tmp_path, path_dirs=[linux.parent],
                          installed=f"$(touch {marker})")
    assert not marker.exists()
    assert rc == 0 and out == str(linux)


def test_spawn_child_sources_the_library_before_it_resolves_codex():
    text = SPAWN.read_text(encoding="utf-8")
    loader = text.index('. "$HOOKS_DIR/codex-bin.sh"')
    assert loader < text.index("\nfind_codex_bin() {")
    assert re.search(r"^codex_bin_problem\(\) \{", LIB.read_text(encoding="utf-8"), re.M)
