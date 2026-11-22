"""nvm resolution, doctor and launch parity without a real Codex or user HOME."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def script(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def fixture(tmp_path, *, node=True, layout="regular"):
    home = tmp_path / "home"
    home.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    # A system with node installed must still reproduce its absence from PATH.
    for name in ("env", "readlink", "sleep", "mktemp", "sed", "cut", "rm", "grep", "tr"):
        (tools / name).symlink_to(shutil.which(name))
    login = script(tmp_path / "login-shell", '#!/bin/bash\nexport PATH="$TEST_MINIMAL_PATH"\nshift\nexec /bin/bash -c "$@"\n')
    prefix = home / ".nvm/versions/node/v24.0.0"
    binary = script(prefix / "bin/codex", '#!/usr/bin/env node\n// synthetic npm entrypoint\n')
    node_dir = binary.parent
    if layout != "regular":
        target = prefix / "lib/node_modules/codex/bin/codex.js"
        target.parent.mkdir(parents=True)
        binary.rename(target)
        binary.symlink_to("../lib/node_modules/codex/bin/codex.js")
        if layout == "target":
            node_dir = target.parent
    if node and layout == "target":
        script(binary.parent / "node", '#!/bin/sh\necho wrong-node >&2\nexit 99\n')
    if node:
        script(node_dir / "node", '#!/bin/sh\nshift\ncase "$1" in\n --version) echo "codex-cli 0.159.2";;\n *) echo "synthetic-launch-ok";;\nesac\n')
    env = dict(HOME=str(home), PATH=str(tools), TEST_MINIMAL_PATH=str(tools),
               AGENTSTACK_CHILD_SHELL=str(login), CODEX_HOME=str(home / "isolated-codex"))
    return env, binary, node_dir


def run(env, body):
    setup = (
        'set -euo pipefail\n'
        f'. {shlex.quote(str(ROOT / "hooks/codex-bin.sh"))}\n'
        'CHILD_SHELL="$(codex_launch_shell)"\n'
        'CODEX_CHILD_PATH_SETUP="$(codex_launch_path_setup)"\n'
        'CODEX_PROBE_RUNNER=codex_launch_runner\nCODEX_LAUNCHER_CONTEXT_READY=1\n'
    )
    return subprocess.run(["/bin/bash", "-c", setup + body], env=env,
                          capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize("layout", ["regular", "npm", "target"])
def test_nvm_selection_probe_and_actual_launch_share_node_path(tmp_path, layout):
    env, binary, node_dir = fixture(tmp_path, layout=layout)
    result = run(env, 'binary="$(codex_find_bin)"\n[[ -n "$binary" ]]\nprintf "%s\\n" "$binary"\ncodex_launch_runner "$binary" --synthetic-launch\n')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(binary), "synthetic-launch-ok"]
    # Run the exact setup embedded in both tmux launch snippets after a login
    # profile has replaced PATH, without registering or starting a child.
    result = run(dict(env, AGENTSTACK_CODEX_BIN=str(binary)),
                 '"$CHILD_SHELL" -lc "$CODEX_CHILD_PATH_SETUP"\'; exec "$0" --synthetic-launch\' "$AGENTSTACK_CODEX_BIN"\n')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "synthetic-launch-ok"


@pytest.mark.parametrize("node", [True, False])
def test_doctor_and_launch_use_same_environment_and_verdict(tmp_path, node):
    env, binary, _ = fixture(tmp_path, node=node)
    source = (ROOT / "scripts/doctor.sh").read_text()
    functions = source[source.index("codex_launcher_search_path() {"):source.index("# GPT-6.1 Sol requires")]
    body = ('problem="$(codex_launcher_problem "$1")"\nprintf "doctor=%s\\n" "$problem"\n'
            'problem="$(codex_bin_problem "$1")"\nprintf "launcher=%s\\n" "$problem"\n')
    result = run(env, functions + "\n" + body.replace('"$1"', shlex.quote(str(binary))))
    # Supply the binary explicitly without inheriting the caller's node/PATH.
    if result.returncode:
        raise AssertionError(result.stderr)
    lines = result.stdout.splitlines()
    if node:
        assert lines == ["doctor=", "launcher="]
    else:
        assert len(lines) == 2 and all("node" in line and "127" in line for line in lines)
        resolution = run(env, 'binary="$(codex_find_bin)"\n[[ -n "$binary" ]]\n')
        assert resolution.returncode != 0
        assert "node" in resolution.stderr and "127" in resolution.stderr


def test_no_adjacent_node_keeps_inherited_node_and_does_not_add_search_dirs(tmp_path):
    env, binary, _ = fixture(tmp_path, node=False)
    result = run(env, f'codex_launch_path {shlex.quote(str(binary))}\n')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{env['HOME']}/.local/bin:{env['PATH']}"


def test_tmux_outer_shell_preserves_the_shared_path_function(tmp_path):
    env, binary, _ = fixture(tmp_path, layout="npm")
    env["AGENTSTACK_CODEX_BIN"] = str(binary)
    source = (ROOT / "hooks/spawn_child.sh").read_text()
    quote_line = next(line for line in source.splitlines()
                      if line.startswith('CODEX_CHILD_PATH_SETUP_QUOTED='))
    # tmux parses this outer command before the child's login shell parses
    # the script. Preserve function strings such as printf '%s\n'.
    result = run(env, quote_line + r'''
command="$CHILD_SHELL -lc '$CODEX_CHILD_PATH_SETUP_QUOTED; exec \"\$AGENTSTACK_CODEX_BIN\" --synthetic-launch'"
/bin/sh -c "$command"
''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "synthetic-launch-ok"


def test_inherited_node_still_runs_a_cli_without_adjacent_node(tmp_path):
    env, binary, _ = fixture(tmp_path, node=False)
    script(Path(env["PATH"]) / "node", '#!/bin/sh\necho inherited-node-ok\n')
    result = run(dict(env, AGENTSTACK_CODEX_BIN=str(binary)),
                 'codex_launch_runner "$AGENTSTACK_CODEX_BIN" --synthetic-launch\n')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "inherited-node-ok"


@pytest.mark.parametrize("already_present", [True, False])
def test_shared_prefix_preserves_python_priority(tmp_path, already_present):
    env, binary, _ = fixture(tmp_path, node=False)
    shared = tmp_path / "shared-prefix/bin"
    shared.mkdir(parents=True)
    binary.rename(shared / "codex")
    binary = shared / "codex"
    script(shared / "node", '#!/bin/sh\nshift\necho shared-node-ok\n')
    script(shared / "python3", '#!/bin/sh\necho wrong-python\n')
    pyenv = tmp_path / "pyenv/shims"
    script(pyenv / "python3", '#!/bin/sh\necho selected-python\n')
    baseline = f"{pyenv}:{env['PATH']}" + (f":{shared}" if already_present else "")
    env.update(PATH=baseline, TEST_MINIMAL_PATH=baseline, AGENTSTACK_CODEX_BIN=str(binary))
    result = run(env, '"$CHILD_SHELL" -lc "$CODEX_CHILD_PATH_SETUP"\'; python3; exec "$0" --synthetic-launch\' "$AGENTSTACK_CODEX_BIN"\n')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["selected-python", "shared-node-ok"]
    result = run(env, f"codex_launch_path {shlex.quote(str(binary))}\n")
    expected = f"{env['HOME']}/.local/bin:{baseline}" + ("" if already_present else f":{shared}")
    assert result.stdout.strip() == expected


def test_nvm_candidate_still_selects_its_node_over_another_version(tmp_path):
    env, binary, _ = fixture(tmp_path)
    other = Path(env["HOME"]) / ".nvm/versions/node/other/bin"
    script(other / "node", '#!/bin/sh\necho wrong-node >&2\nexit 99\n')
    baseline = f"{other}:{env['PATH']}:{binary.parent}"
    env.update(PATH=baseline, TEST_MINIMAL_PATH=baseline, AGENTSTACK_CODEX_BIN=str(binary))
    result = run(env, 'codex_launch_runner "$AGENTSTACK_CODEX_BIN" --synthetic-launch\n')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "synthetic-launch-ok"


def test_doctor_prefers_its_own_helper_over_an_old_install(tmp_path):
    env, binary, _ = fixture(tmp_path)
    script_dir = tmp_path / "checkout/scripts"
    script_dir.mkdir(parents=True)
    hooks = script_dir.parent / "hooks"
    hooks.mkdir()
    shutil.copyfile(ROOT / "hooks/codex-bin.sh", hooks / "codex-bin.sh")
    installed = tmp_path / "installed"
    script(installed / "hooks/codex-bin.sh", 'echo stale-helper-was-loaded >&2\n')
    source = (ROOT / "scripts/doctor.sh").read_text()
    loader = source[source.index('CODEX_HELPER="$SCRIPT_DIR/../hooks/codex-bin.sh"'):source.index("# GPT-6.1 Sol requires")]
    body = (f"SCRIPT_DIR={shlex.quote(str(script_dir))}\nINSTALL_DIR={shlex.quote(str(installed))}\n"
            + loader + f'\n[[ -z "$(codex_launcher_problem {shlex.quote(str(binary))})" ]]\n')
    # Run without preloading this checkout's functions; use the actual doctor loader.
    result = subprocess.run(["/bin/bash", "-c", "set -euo pipefail\n" + body], env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "stale-helper" not in result.stdout + result.stderr


def test_doctor_warns_before_probing_with_incompatible_helper(tmp_path):
    env, _, _ = fixture(tmp_path)
    installed = tmp_path / "old-install"
    script(installed / "hooks/codex-bin.sh", 'codex_find_bin() { echo must-not-be-used; }\n')
    source = (ROOT / "scripts/doctor.sh").read_text()
    loader = source[source.index('CODEX_HELPER="$SCRIPT_DIR/../hooks/codex-bin.sh"'):source.index("# GPT-6.1 Sol requires")]
    body = (f"SCRIPT_DIR={shlex.quote(str(tmp_path / 'missing/scripts'))}\nINSTALL_DIR={shlex.quote(str(installed))}\n"
            + loader + '\nresolve_launcher_codex_bin\n')
    result = subprocess.run(["/bin/bash", "-c", "set -euo pipefail\n" + body], env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "checks unavailable" in result.stdout
    assert "must-not-be-used" not in result.stdout
    assert "syntax error" not in result.stdout + result.stderr


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_shared_prefix_order_is_portable_across_child_shells(tmp_path, shell):
    shell_path = shutil.which(shell)
    if not shell_path:
        pytest.skip(f"{shell} is unavailable")
    env, binary, _ = fixture(tmp_path, node=False)
    shared = tmp_path / "shared/bin"
    shared.mkdir(parents=True)
    binary.rename(shared / "codex")
    script(shared / "node", '#!/bin/sh\necho shared-node-ok\n')
    script(shared / "python3", '#!/bin/sh\necho wrong-python\n')
    chosen = tmp_path / "pyenv/shims"
    script(chosen / "python3", '#!/bin/sh\necho chosen-python\n')
    env.update(PATH=f"{chosen}:{shared}:{env['PATH']}", AGENTSTACK_CODEX_BIN=str(shared / "codex"))
    setup = run(env, "codex_launch_path_setup\n")
    assert setup.returncode == 0, setup.stderr
    result = subprocess.run([shell_path, "-c", setup.stdout + '\npython3\n"$AGENTSTACK_CODEX_BIN" --synthetic-launch\n'],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["chosen-python", "shared-node-ok"]
