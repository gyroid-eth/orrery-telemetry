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


def _runner() -> str:
    """The child PATH setup and the probe runner, as spawn_child.sh defines them."""
    text = SPAWN.read_text(encoding="utf-8")
    start = text.index("CODEX_CHILD_PATH_SETUP=")
    end = text.index("CODEX_PROBE_RUNNER=run_like_codex_child\n", start) + len("CODEX_PROBE_RUNNER=run_like_codex_child\n")
    return text[start:end]


def _resolve(tmp_path, *, path_dirs, env_bin=None, installed=None, wsl=False, lib=True, runner=True):
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
        + ("CHILD_SHELL=/bin/bash\n" + _runner() if runner else "")
        + loader
        # Stub the platform after the library is loaded; /mnt cannot exist on macOS CI.
        + ("running_under_wsl() { return 0; }\n" if wsl else "running_under_wsl() { return 1; }\n")
        + f"WSL_WINDOWS_MOUNT_ROOT={shlex.quote(str(tmp_path / 'mnt'))}\n"
        + "CODEX_VERSION_TIMEOUT_SECONDS=10\n"
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


def test_the_probe_runs_codex_with_the_path_the_child_will_have(tmp_path):
    """Pink's reproduction (review of 55cd037): the launcher's PATH is minimal,
    and ~/.local/bin holds both codex (`#!/usr/bin/env node`) and node. The child
    runs codex from a login shell with ~/.local/bin in front, so it works there;
    a probe under the launcher's own PATH could not find node and rejected it."""
    local_bin = tmp_path / "home" / ".local" / "bin"
    _script(local_bin / "node", "#!/bin/sh\necho v22.0.0\n")
    codex = _script(local_bin / "codex", "#!/usr/bin/env node\n")
    rc, out, err = _resolve(tmp_path, path_dirs=[])
    assert rc == 0, err
    assert out == str(codex)
    # Probing with the launcher's PATH instead would reject it.
    rc, _, err = _resolve(tmp_path, path_dirs=[], runner=False)
    assert rc == 1 and "--version' did not succeed" in err


def test_both_codex_launches_use_the_one_path_setup():
    text = SPAWN.read_text(encoding="utf-8")
    assert text.count("\'\"$CODEX_CHILD_PATH_SETUP\"\';") == 2
    runner = text[text.index("run_like_codex_child() {"):text.index("CODEX_PROBE_RUNNER=run_like_codex_child")]
    assert '"$CHILD_SHELL" -lc "$CODEX_CHILD_PATH_SETUP"' in runner
    # The approval probe (--help) goes through the same runner.
    approval = text[text.index("codex_approval_flags() {"):]
    assert '${CODEX_PROBE_RUNNER:-} "$codex_bin" --help' in approval[:approval.index("\n}\n")]


# --- one resolution per spawn, bounded in time ----------------------------------

import time  # noqa: E402

HANGS = '#!/bin/sh\nif [ "$1" = --version ]; then echo "$1" >> "$PROBE_LOG"; sleep 30; fi\necho "  --ask-for-approval <POLICY>"\n'


# Probe limits in these tests leave room for the login shell each probe
# starts (it runs the way the child will): 2-3s lost to shell start-up on a
# busy machine (MintHooke, review of 0c87e5e).
def _spawn_resolution(tmp_path, *, path_dirs, installed=None, timeout=5, budget=None):
    """prime_codex_bin, then the approval probe and resolve_codex_bin, as one
    spawn runs them; returns (rc, stdout lines, stderr, seconds)."""
    home = tmp_path / "home"
    (home / ".agentstack").mkdir(parents=True, exist_ok=True)
    if installed is not None:
        (home / ".agentstack" / "env.sh").write_text(
            f"export AGENTSTACK_CODEX_BIN={shlex.quote(installed)}\n", encoding="utf-8")
    text = SPAWN.read_text(encoding="utf-8")
    start = text.index("# Codex candidate rules, and the env.sh reader")
    loader = text[start:text.index("codex_search_path() {", start)]
    script = (
        f"HOOKS_DIR={shlex.quote(str(ROOT / 'hooks'))}\nCHILD_SHELL=/bin/bash\n" + _runner() + loader
        + "running_under_wsl() { return 1; }\n"
        + f"CODEX_VERSION_TIMEOUT_SECONDS={timeout}\n"
        + (f"CODEX_PROBE_BUDGET_SECONDS={budget}\n" if budget is not None else "")
        + _extract("codex_search_path") + _extract("find_codex_bin") + _extract("prime_codex_bin")
        + _extract("resolve_codex_bin") + _extract("codex_approval_flags")
        + "prime_codex_bin\nprintf 'APPROVAL=%s\\n' \"$(codex_approval_flags)\"\n"
        + "printf 'CODEX=%s\\n' \"$(resolve_codex_bin)\"\n"
    )
    env = {"HOME": str(home), "PATH": ":".join(str(d) for d in path_dirs) + ":/usr/bin:/bin",
           "PROBE_LOG": str(tmp_path / "probes")}
    started = time.monotonic()
    result = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=120)
    return result.returncode, result.stdout.splitlines(), result.stderr, time.monotonic() - started


def test_an_unresponsive_saved_codex_is_probed_once_per_spawn(tmp_path):
    # MintHooke's case: the same hanging codex saved in env.sh and first on
    # PATH, a working one after it. Before, approval and resolve each probed it
    # from both sources: four timeouts.
    bad = _script(tmp_path / "bad" / "codex", HANGS)
    good = _script(tmp_path / "good" / "codex", WORKS)
    rc, lines, err, seconds = _spawn_resolution(tmp_path, path_dirs=[bad.parent, good.parent], installed=str(bad))
    assert rc == 0, err
    assert f"CODEX={good}" in lines
    assert (tmp_path / "probes").read_text().count("--version") == 1
    assert err.count(f"({bad})") == 1  # one skip note, from env.sh
    # The probe count is the proof; the time bound only catches a runaway
    # (four 5s timeouts plus shells) with room for a loaded machine.
    assert seconds < 18, seconds


def test_all_probes_share_one_budget(tmp_path):
    dirs = [_script(tmp_path / f"slow{i}" / "codex", HANGS).parent for i in range(5)]
    good = _script(tmp_path / "good" / "codex", WORKS)
    rc, lines, err, seconds = _spawn_resolution(tmp_path, path_dirs=[*dirs, good.parent], timeout=5, budget=8)
    # Five 5-second timeouts would take 25s; the 8-second budget stops after
    # at most two probes. The count is the proof; the time bound only catches
    # a runaway.
    assert "budget for trying codex candidates is spent" in err
    assert (tmp_path / "probes").read_text().count("--version") <= 2
    assert seconds < 20, seconds
    assert rc == 0 and "CODEX=" in "".join(lines)  # resolve reports, it does not hang


def test_the_budgets_fit_inside_the_dashboard_launcher_limit():
    """In numbers: the dashboard signals a launcher after 120s
    (dashboard/server.py _SPAWN_READINESS_TIMEOUT_SECONDS), and its exit trap
    then removes the child. The codex resolution is bounded by its budget, and
    the start watch ends by CODEX_WATCH_END_BY seconds of the launcher's run
    whatever preceded it, leaving room for its last poll (3s) and note."""
    lib = LIB.read_text(encoding="utf-8")
    spawn = SPAWN.read_text(encoding="utf-8")
    server = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
    dashboard_limit = int(re.search(r"^_SPAWN_READINESS_TIMEOUT_SECONDS = (\d+)", server, re.M).group(1))
    budget = int(re.search(r"^CODEX_PROBE_BUDGET_SECONDS=(\d+)", lib, re.M).group(1))
    per_probe = int(re.search(r"^CODEX_VERSION_TIMEOUT_SECONDS=(\d+)", lib, re.M).group(1))
    watch_end = int(re.search(r"^CODEX_WATCH_END_BY=(\d+)", spawn, re.M).group(1))
    poll = 3
    assert per_probe <= budget
    assert budget < watch_end
    assert watch_end + poll + 5 <= dashboard_limit


def test_the_start_watch_ends_by_its_launcher_deadline_after_a_slow_start(tmp_path):
    # The launcher has already run 100s (slow registration, probes): the watch
    # may only use what is left before CODEX_WATCH_END_BY, not its full 90s.
    names = ("pane_nonblank_tail", "pane_normalize_nbsp", "codex_trust_screen_up", "codex_watch_initial_task")
    functions = "".join(_extract(name) for name in names)
    script = (
        "CODEX_WATCH_END_BY=105\nINJECTION_VERIFIED=false\n"
        f"POLLS={shlex.quote(str(tmp_path / 'polls'))}\n"
        "tmux() { :; }\n"
        "codex_session_alive() { return 0; }\n"
        "codex_initial_task_status() { echo unknown; }\n"
        "spawn_note() { :; }\n"
        # Each poll's sleep advances the launcher clock by what it sleeps.
        'sleep() { [[ "$1" == 3 ]] && echo poll >> "$POLLS"; SECONDS=$((SECONDS + ${1%%.*})); }\n'
        + functions
        + "SECONDS=100\nstatus=0; codex_watch_initial_task Child task test /l id || status=$?\n"
        'echo "STATUS=$status END=$SECONDS"\n'
    )
    result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert "STATUS=3" in result.stdout, result.stdout + result.stderr
    end = int(result.stdout.split("END=")[1])
    assert end <= 105 + 3
    assert (tmp_path / "polls").read_text().count("poll") <= 2


def test_the_probe_shell_has_the_guards_the_child_session_has(tmp_path):
    """Pink's reproduction (review of 0c87e5e): a profile that behaves
    differently without CLAUDECODE=1 (here: exits 23) must see the same
    guard in the probe as in the child's session."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bash_profile").write_text(
        '[ "$CLAUDECODE" = 1 ] && [ "$AGENTSTACK_RESERVED_IDENTITY" = 1 ] || exit 23\n', encoding="utf-8")
    codex = _script(tmp_path / "bin" / "codex", WORKS)
    script = "CHILD_SHELL=/bin/bash\n" + _runner() + f"run_like_codex_child {shlex.quote(str(codex))} --version\n"
    result = subprocess.run(["/bin/bash", "-c", script], env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    # The same guard values as the child's tmux session.
    text = SPAWN.read_text(encoding="utf-8")
    assert text.count('TMUX_ENV_ARGS=(-e "CLAUDECODE=1" -e "AGENTSTACK_RESERVED_IDENTITY=1"') == 2
