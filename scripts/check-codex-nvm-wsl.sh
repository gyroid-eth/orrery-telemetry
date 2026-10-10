#!/bin/bash
# Existing CLI only; no install, login, model/thread call or child registration.
set -euo pipefail
if [[ $# -lt 1 || $# -gt 3 ]]; then
    echo "usage: $0 /absolute/path/to/codex [python3] [/absolute/path/to/node]" >&2
    exit 2
fi
cli="$1"
python="${2:-python3}"
node="${3:-$(command -v node || true)}"
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$python" - "$repo" "$cli" "$node" <<'PY'
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11+ is required")
repo, cli, node = map(Path, sys.argv[1:])
if not cli.is_absolute() or not cli.is_file() or not os.access(cli, os.X_OK):
    raise SystemExit("supply an absolute path to an existing Codex CLI")
if not node.is_absolute() or not node.is_file() or not os.access(node, os.X_OK):
    raise SystemExit("supply an existing node as argument 3 (no binary is installed)")
with tempfile.TemporaryDirectory(prefix="orrery-codex-nvm-") as tmp:
    home = Path(tmp)
    tools = home / "tools"
    tools.mkdir()
    for name in ("env", "readlink", "sleep", "mktemp", "sed", "cut", "rm", "grep", "tr"):
        source = shutil.which(name)
        if not source:
            raise SystemExit(f"missing system tool: {name}")
        (tools / name).symlink_to(source)
    login = home / "login-shell"
    login.write_text('#!/bin/bash\nexport PATH="$TEST_MINIMAL_PATH"\nshift\nexec /bin/bash --noprofile --norc -c "$@"\n')
    login.chmod(0o755)
    # npm-shaped env-node entrypoint. It only delegates --version to the
    # existing CLI, including when that CLI is a standalone binary.
    entry = home / ".nvm/versions/node/test/lib/codex.js"
    entry.parent.mkdir(parents=True)
    entry.write_text('''#!/usr/bin/env node
const result = require('child_process').spawnSync(process.env.TEST_REAL_CODEX, ['--version'], {stdio: 'inherit', env: process.env});
if (result.error) { console.error(result.error.message); process.exit(1); }
process.exit(result.status === null ? 1 : result.status);
''')
    entry.chmod(0o755)
    bin_dir = home / ".nvm/versions/node/test/bin"
    bin_dir.mkdir()
    (bin_dir / "codex").symlink_to("../lib/codex.js")
    (bin_dir / "node").symlink_to(node)
    # Deliberately exclude all inherited settings, credentials and user PATH.
    env = dict(HOME=str(home), PATH=str(tools), TMPDIR=str(home),
               CODEX_HOME=str(home / "codex-home"), NVM_DIR=str(home / ".nvm"),
               AGENTSTACK_CHILD_SHELL=str(login), TEST_MINIMAL_PATH=str(tools),
               AGENTSTACK_CODEX_BIN=str(bin_dir / "codex"),
               TEST_REAL_CODEX=str(cli))
    Path(env["CODEX_HOME"]).mkdir()
    source = (repo / "scripts/doctor.sh").read_text()
    doctor = source[source.index("codex_launcher_search_path() {"):source.index("# GPT-6.1 Sol requires")]
    setup = ('set -euo pipefail\n'
             f'. {shlex.quote(str(repo / "hooks/codex-bin.sh"))}\n'
             'CHILD_SHELL="$(codex_launch_shell)"\n'
             'CODEX_CHILD_PATH_SETUP="$(codex_launch_path_setup)"\n'
             'CODEX_PROBE_RUNNER=codex_launch_runner\n')
    def check(body, good=True):
        proc = subprocess.Popen(["/bin/bash", "-c", setup + doctor + "\n" + body],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except BaseException:
            # This process group belongs solely to this disposable probe.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate(timeout=5)
            raise
        result = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
        if (result.returncode == 0) != good:
            print(result.stdout, end="")
            print(result.stderr, end="", file=sys.stderr)
            raise SystemExit("isolated probe had an unexpected result")
        return result
    selected = check('bin="$(codex_find_bin)"\n[[ "$bin" == "$HOME/.nvm/versions/node/test/bin/codex" ]]\ncodex_launch_runner "$bin" --version\n')
    doctor_result = check('bin="$(resolve_launcher_codex_bin)"\n[[ -n "$bin" ]]\n[[ -z "$(codex_launcher_problem "$bin")" ]]\n')
    # The exact script embedded in tmux, executed without creating a session.
    env["AGENTSTACK_CODEX_BIN"] = str(bin_dir / "codex")
    spawn_source = (repo / "hooks/spawn_child.sh").read_text()
    quote_line = next(line for line in spawn_source.splitlines()
                      if line.startswith("CODEX_CHILD_PATH_SETUP_QUOTED="))
    launched = check(quote_line + r'''
command="$CHILD_SHELL -lc '$CODEX_CHILD_PATH_SETUP_QUOTED; exec \"\$AGENTSTACK_CODEX_BIN\" --version'"
/bin/sh -c "$command"
''')
    (bin_dir / "node").unlink()
    missing = check('problem="$(codex_launcher_problem "$AGENTSTACK_CODEX_BIN")"\n[[ -z "$problem" ]]\n', good=False)
    rejected = check('bin="$(find_usable_codex_bin_in "${AGENTSTACK_CODEX_BIN%/*}")"\n[[ -n "$bin" ]]\n', good=False)
    if "node" not in rejected.stderr or "127" not in rejected.stderr:
        raise SystemExit("missing node was not explained")
    print(json.dumps(dict(phase="complete", probe_version=selected.stdout.strip(),
                         launch_version=launched.stdout.strip(), doctor_matches=True,
                         missing_node_rejected=True, isolated_home=True,
                         model_thread_login_calls=False, network_blocked=False,
                         note="version only; no network namespace was requested")))
PY
