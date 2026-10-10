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
        'CODEX_PROBE_RUNNER=codex_launch_runner\n'
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
