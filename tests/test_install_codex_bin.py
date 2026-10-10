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
import re
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


def _helper_load_block() -> str:
    text = INSTALL.read_text(encoding="utf-8")
    start = text.index("# --- codex helper load")
    end = text.index("# --- end codex helper load ---", start) + len("# --- end codex helper load ---\n")
    return text[start:end]


def _dashboard_only_block() -> str:
    # The TIER guard around the helper load, through the codex resolution's
    # own TIER guard and its final fi: this is the whole span --dashboard-only
    # must get through without hooks/codex-bin.sh existing at all (#243).
    text = INSTALL.read_text(encoding="utf-8")
    start = text.index("# --dashboard-only copies no hooks/")
    end = text.index("# A Node-installed `codex` is a wrapper", start)
    return text[start:end]


_PROBE_FN = "echo LOADED=$(declare -F codex_bin_problem >/dev/null 2>&1 && echo yes || echo no)\necho HELPER=$CODEX_HELPER\n"


def test_codex_helper_loads_from_the_checkout(tmp_path):
    # install.sh only ever runs from its own checkout (it copies
    # $REPO_ROOT/hooks to the install destination, never the reverse), so
    # $REPO_ROOT/hooks/codex-bin.sh is the only copy it reads (#243).
    repo_root = tmp_path / "checkout"
    (repo_root / "hooks").mkdir(parents=True)
    (repo_root / "hooks" / "codex-bin.sh").write_text(
        (ROOT / "hooks" / "codex-bin.sh").read_text(encoding="utf-8"), encoding="utf-8")
    install_dir = tmp_path / "install"  # not yet populated; must not be needed
    script = (
        f"REPO_ROOT={shlex.quote(str(repo_root))}\n"
        f"INSTALL_DIR={shlex.quote(str(install_dir))}\n"
        + _helper_load_block() + "\n" + _PROBE_FN
    )
    result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "LOADED=yes" in result.stdout
    assert f"HELPER={repo_root}/hooks/codex-bin.sh" in result.stdout


def test_codex_helper_missing_from_the_checkout_stops_instead_of_skipping_detection(tmp_path):
    # Even with an already-installed hooks/ elsewhere, install.sh must not
    # fall back to it: that copy can only be stale relative to this checkout,
    # and if the checkout's own hooks/codex-bin.sh is missing, the later copy
    # step that would need it is already broken (#243).
    repo_root = tmp_path / "checkout-without-hooks"
    repo_root.mkdir()
    install_dir = tmp_path / "install"
    (install_dir / "hooks").mkdir(parents=True)
    (install_dir / "hooks" / "codex-bin.sh").write_text(
        (ROOT / "hooks" / "codex-bin.sh").read_text(encoding="utf-8"), encoding="utf-8")
    script = (
        f"REPO_ROOT={shlex.quote(str(repo_root))}\n"
        f"INSTALL_DIR={shlex.quote(str(install_dir))}\n"
        + _helper_load_block()
    )
    result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "missing Codex launcher helper" in result.stderr
    assert str(repo_root / "hooks" / "codex-bin.sh") in result.stderr


def test_codex_helper_missing_required_function_stops_instead_of_skipping_detection(tmp_path):
    # A helper file is present but incompatible (e.g. a much older copy):
    # installer must not run with half the shared contract missing.
    repo_root = tmp_path / "checkout"
    (repo_root / "hooks").mkdir(parents=True)
    (repo_root / "hooks" / "codex-bin.sh").write_text(
        "#!/bin/bash\ncodex_bin_problem() { :; }\n", encoding="utf-8")
    install_dir = tmp_path / "install"
    script = (
        f"REPO_ROOT={shlex.quote(str(repo_root))}\n"
        f"INSTALL_DIR={shlex.quote(str(install_dir))}\n"
        + _helper_load_block()
    )
    result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "missing required function" in result.stderr


def test_dashboard_only_does_not_require_the_codex_helper_to_exist(tmp_path):
    # --dashboard-only copies no hooks/ and never launches a Codex child, so
    # it must get through Codex resolution without hooks/codex-bin.sh
    # existing at all — not just without it being missing-but-checked (#243).
    # A test_dashboard_service.py installer-repo fixture built for this tier
    # legitimately omits hooks/codex-bin.sh; this must not become "error:
    # missing Codex launcher helper" before whatever check that fixture is
    # actually exercising gets to run.
    repo_root = tmp_path / "checkout-without-hooks"
    repo_root.mkdir()
    install_dir = tmp_path / "install"
    script = (
        f"REPO_ROOT={shlex.quote(str(repo_root))}\n"
        f"INSTALL_DIR={shlex.quote(str(install_dir))}\n"
        "TIER=tier0\n"
        "RESET_SETTINGS=0\nOPTION_GIVEN=\nCODEX_BIN_SETTING=''\n"
        "agentstack_installed_env_value() { printf '%s\\n' ''; }\n"
        "SETTING_SOURCE=''\n"
        "resolve_setting() { SETTING_SOURCE=default; printf -v \"$1\" '%s' \"\"; }\n"
        + _dashboard_only_block()
        + '\nprintf "RESOLVED=%s\\n" "$CODEX_BIN_SETTING"\n'
        + 'printf "NOTE=%s\\n" "$CODEX_CONTEXT_NOTE"\n'
    )
    result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "RESOLVED=\n" in result.stdout
    assert "NOTE=not checked: --dashboard-only does not use Codex" in result.stdout
    assert "missing Codex launcher helper" not in result.stderr


def _script(path: pathlib.Path, body: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _raw_script(path: pathlib.Path, body: str) -> pathlib.Path:
    # Unlike _script, does not prepend a shebang: the caller's first line
    # (e.g. "#!/usr/bin/env node") is the one the kernel actually reads.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


WORKS = 'echo "codex-cli 0.157.0"\n'
BROKEN = 'echo "Error: Missing optional dependency @openai/codex-linux-x64" >&2\nexit 1\n'


def _resolve(tmp_path, *, path_dirs, wsl=False, explicit="", installed="", timeout=5, budget=15, extra_env=None,
             path_suffix=":/usr/bin:/bin"):
    """Run the block; returns (exit code, resolved CODEX_BIN_SETTING, stderr).

    The default probe budget is generous so a working fake codex is not failed
    by a loaded machine; the hang and budget-exhaustion tests pass a short
    timeout/budget explicitly.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    text = INSTALL.read_text(encoding="utf-8")
    stubs = (
        "set -euo pipefail\n"
        f". {shlex.quote(str(INSTALL.parents[1] / 'hooks' / 'project-context.sh'))}\n"
        f"REPO_ROOT={shlex.quote(str(INSTALL.parents[1]))}\n"
        f"INSTALL_DIR={shlex.quote(str(tmp_path / 'install'))}\n"
        # The resolution block now only wraps hooks/codex-bin.sh's shared
        # functions (#242); the helper-load block (which it depends on) sets
        # them up the same way install.sh itself does, including the
        # child-login-shell probe context (#243).
        + _helper_load_block() + "\n"
        # install.sh's own top-of-script default (TIER="tier1"); the block
        # under test reads it to skip Codex entirely for --dashboard-only (#243).
        "TIER=tier1\n"
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
        + f"CODEX_PROBE_BUDGET_SECONDS={budget}\n"
    )
    marker = "# --- end codex launcher resolution ---\n"
    block = block.replace(marker, marker + overrides, 1)
    script = stubs + block + '\nprintf "RESOLVED=%s\\n" "$CODEX_BIN_SETTING"\n'
    env = {
        "HOME": str(home),
        "PATH": ":".join(str(d) for d in path_dirs) + path_suffix,
    }
    if extra_env:
        env.update(extra_env)
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
    # budget is generous: this is about the per-candidate timeout, not the
    # shared probe budget, and the kill/reap wait eats into that budget too.
    _, resolved, _ = _resolve(tmp_path, path_dirs=[hung.parent, linux.parent], timeout=1, budget=90)
    assert resolved == str(linux)


def test_the_per_user_npm_prefix_is_found_when_not_on_path(tmp_path):
    # WSL docs install Codex under ~/.npm-global/bin, which a fresh shell may lack.
    windows = _windows_codex(tmp_path)
    npm_global = _script(tmp_path / "home" / ".npm-global" / "bin" / "codex", WORKS)
    _, resolved, _ = _resolve(tmp_path, path_dirs=[windows.parent], wsl=True)
    assert resolved == str(npm_global)


def _login_shell_with_fixed_path(tmp_path, minimal_path) -> pathlib.Path:
    # Overrides codex_launch_shell's own pick (AGENTSTACK_CHILD_SHELL): the
    # spawned child's actual login profile sets a PATH independent of this
    # test process's own, which is exactly the gap #243 found (the installer
    # judged a candidate under its own PATH instead of this one).
    return _script(tmp_path / "login-shell",
                    f'export PATH={shlex.quote(str(minimal_path))}\nshift\nexec /bin/bash -c "$@"\n')


def test_an_nvm_npm_style_codex_is_selected_the_same_way_the_launcher_picks_it(tmp_path):
    # hooks/codex-bin.sh's own nvm search dir, not path_dirs: codex_launch_path
    # must put this bin dir (it has an adjacent node) ahead of the child's own
    # PATH for the probe to answer, the same way spawn_child.sh and doctor.sh
    # already do (#239). Before #243 the installer probed under its own PATH
    # and never looked here at all.
    home = tmp_path / "home"
    nvm_bin = home / ".nvm" / "versions" / "node" / "v24.0.0" / "bin"
    codex = _raw_script(nvm_bin / "codex", "#!/usr/bin/env node\n")
    _script(nvm_bin / "node", 'shift\ncase "$1" in\n --version) echo "codex-cli 0.159.2";;\nesac\n')
    minimal = tmp_path / "minimal-path"
    minimal.mkdir()
    login = _login_shell_with_fixed_path(tmp_path, minimal)
    code, resolved, stderr = _resolve(
        tmp_path, path_dirs=[],
        extra_env={"AGENTSTACK_CHILD_SHELL": str(login)})
    assert code == 0, stderr
    assert resolved == str(codex)


def test_an_npm_global_codex_without_adjacent_node_is_rejected_the_same_way_the_launcher_rejects_it(tmp_path):
    # node sits on this test process's own PATH (what the installer's probe
    # used before #243) but not on the child login shell's PATH (what the
    # dashboard's spawned Codex child actually gets). A candidate accepted
    # here and rejected at launch time is the #239 symptom; the installer must
    # now reject it up front too, matching doctor and the launcher.
    home = tmp_path / "home"
    codex = _raw_script(home / ".npm-global" / "bin" / "codex", "#!/usr/bin/env node\n")
    node_only_on_this_path = tmp_path / "only-this-process-sees-this"
    _script(node_only_on_this_path / "node", 'shift\ncase "$1" in\n --version) echo "codex-cli 0.159.2";;\nesac\n')
    minimal = tmp_path / "minimal-path"
    minimal.mkdir()
    login = _login_shell_with_fixed_path(tmp_path, minimal)
    code, resolved, stderr = _resolve(
        tmp_path, path_dirs=[node_only_on_this_path],
        extra_env={"AGENTSTACK_CHILD_SHELL": str(login)})
    assert code == 0, stderr
    assert resolved == ""
    assert "env: node" in stderr or "No such file" in stderr or "note: skipping" in stderr


def test_nothing_usable_leaves_the_setting_empty(tmp_path):
    windows = _windows_codex(tmp_path)
    code, resolved, stderr = _resolve(tmp_path, path_dirs=[windows.parent], wsl=True)
    assert code == 0 and resolved == ""


def test_no_login_shell_skips_detection_without_saving_a_new_value(tmp_path):
    # Nothing saved, no zsh/bash on PATH at all: there is no context to judge
    # a candidate under, so none is searched for and none is saved (#243) —
    # not a silent fallback to a direct, installer-only PATH probe.
    working = _script(tmp_path / "linux-bin" / "codex", WORKS)
    code, resolved, stderr = _resolve(tmp_path, path_dirs=[working.parent], path_suffix="")
    assert code == 0
    assert resolved == ""
    assert "not checked: no login shell" in stderr
    assert "note: Codex launcher checks skipped" in stderr


def test_no_login_shell_leaves_a_saved_value_untouched(tmp_path):
    # A saved value is not "stale" just because this run cannot judge
    # anything: it was never checked this run, so it must not be cleared,
    # re-searched or re-saved.
    saved = tmp_path / "saved-bin" / "codex"
    code, resolved, stderr = _resolve(
        tmp_path, path_dirs=[], installed=str(saved), path_suffix="")
    assert code == 0
    assert resolved == str(saved)
    assert "not checked: no login shell" in stderr
    assert "is stale" not in stderr


def test_no_login_shell_stops_an_explicit_codex_bin_instead_of_skipping_it(tmp_path):
    # An explicit --codex-bin / AGENTSTACK_CODEX_BIN is a direct request:
    # unlike the auto/saved cases, it must not be silently left unchecked.
    explicit = tmp_path / "explicit-bin" / "codex"
    code, resolved, stderr = _resolve(
        tmp_path, path_dirs=[], explicit=str(explicit), path_suffix="")
    assert code == 2
    assert f"cannot be used: {explicit}" not in stderr  # not probed, so no probe reason
    assert "cannot be used: no login shell" in stderr
    # Every candidate was judged and rejected for a real reason; this must not
    # read like the budget-exhaustion case below, where some were never tried.
    assert "candidate-probe budget was spent" not in stderr


def test_slow_candidates_exhaust_the_probe_budget_before_a_working_one(tmp_path):
    # Two candidates ahead of a working one are each slow enough that the
    # shared probe budget (not any one candidate's own timeout) runs out
    # first. The working one is never tried, so it must not resolve, and the
    # installer must say that detection was cut short -- not that nothing was
    # found (#242).
    hung_1 = _script(tmp_path / "hung-1-bin" / "codex", "sleep 30\n")
    hung_2 = _script(tmp_path / "hung-2-bin" / "codex", "sleep 30\n")
    working = _script(tmp_path / "linux-bin" / "codex", WORKS)
    code, resolved, stderr = _resolve(
        tmp_path,
        path_dirs=[hung_1.parent, hung_2.parent, working.parent],
        timeout=10,
        budget=1,
    )
    assert code == 0
    assert resolved == ""
    assert "Codex detection stopped after the 1s candidate-probe budget was spent" in stderr
    assert "AGENTSTACK_CODEX_BIN is left unset" in stderr
    assert "--codex-bin" in stderr
    assert "not probed: the 1s budget for trying codex candidates is spent" in stderr


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
    assert re.search(r"--version' exited with status 1 after \d+s:", stderr)
    assert "Missing optional dependency @openai/codex-linux-x64" in stderr
    assert "within" not in stderr


def test_a_stale_installed_candidate_also_on_path_is_judged_only_once(tmp_path):
    # A stale saved value that also happens to be the first candidate on PATH
    # must be probed once, under one shared budget, not once unbudgeted for
    # the stale check and again (its own fresh budget) for the PATH scan
    # (#243). timeout=2 keeps a single probe fast; two would push this well
    # past the 3s bound below.
    hung = _script(tmp_path / "hung-bin" / "codex", "sleep 30\n")
    linux = _script(tmp_path / "linux-bin" / "codex", WORKS)
    started = time.monotonic()
    _, resolved, stderr = _resolve(
        tmp_path, path_dirs=[hung.parent, linux.parent], installed=str(hung), timeout=2, budget=90)
    elapsed = time.monotonic() - started
    assert resolved == str(linux)
    assert elapsed < 3, f"took {elapsed}s: the stale candidate looks like it was probed twice"
    # Already judged (as the stale installed value): the PATH scan must skip
    # it silently, not probe it again and note it as a fresh rejection.
    assert f"skipping codex {hung}" not in stderr


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
    # budget is generous: this test is about the stop/cleanup path reaching
    # the next candidate, not about the shared probe budget (see
    # test_slow_candidates_exhaust_the_probe_budget_before_a_working_one for
    # that), and the cleanup itself eats into the shared budget. timeout=2,
    # not 1: hooks/codex-bin.sh bounds the probe with the whole-second `SECONDS`
    # builtin, so a 1s timeout can fire after under 1s of real wait when the
    # probe starts a hair before a tick; 2s keeps clear of that floor.
    _, resolved, _ = _resolve(tmp_path, path_dirs=[stubborn.parent, linux.parent], timeout=2, budget=90)
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
        # budget and timeout are generous for the same reasons as the test above.
        _, resolved, _ = _resolve(tmp_path, path_dirs=[wrapper.parent, linux.parent], timeout=2, budget=90)
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
    # install.sh and doctor.sh both share codex_probe_stop / codex_process_start
    # from hooks/codex-bin.sh now (#242); `script` is kept as a parametrize
    # label so a regression in either caller's use of them still shows up.
    source = ROOT / "hooks/codex-bin.sh"
    text = source.read_text(encoding="utf-8")
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
    assert re.search(r"--version' exited with status 7 after \d+s:", stderr)
    assert "(no error output)" in stderr
