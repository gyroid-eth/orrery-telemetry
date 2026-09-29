"""End-to-end shell reproduction for a fresh-directory Claude trust gate."""
from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from service_teardown import TEST_LABEL_PREFIX  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPAWN = ROOT / "hooks" / "spawn_child.sh"


def _executable(path: pathlib.Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_fresh_workdir_trust_prompt_is_accepted_without_waiting_out_timeout(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    runtime = tmp_path / "runtime"
    workdir = tmp_path / "code" / "test-project"
    workdir.mkdir(parents=True)
    tmux_log = tmp_path / "tmux.log"
    tmux_alive = tmp_path / "tmux.alive"
    trust_accepted = tmp_path / "trust.accepted"

    _executable(
        bindir / "tmux",
        "#!/bin/bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_TMUX_LOG\"\n"
        "case \"$1\" in\n"
        "  new-session) : > \"$FAKE_TMUX_ALIVE\" ;;\n"
        "  capture-pane)\n"
        "    if [[ -f \"$FAKE_TRUST_ACCEPTED\" ]]; then printf '\\n❯ \\n';\n"
        "    else printf 'Do you trust the files in this folder?\\n  Yes\\n  No\\n'; fi ;;\n"
        "  has-session) [[ -f \"$FAKE_TMUX_ALIVE\" ]] ;;\n"
        "  send-keys)\n"
        "    [[ \" $* \" == *' C-m '* || \"$*\" == *' C-m' ]] && : > \"$FAKE_TRUST_ACCEPTED\"\n"
        "    true ;;\n"
        "  kill-session) rm -f \"$FAKE_TMUX_ALIVE\" ;;\n"
        "  display-message) printf 'ParentAgent\\n' ;;\n"
        "esac\n",
    )
    _executable(bindir / "sleep", "#!/bin/bash\nexit 0\n")
    _executable(
        bindir / "codex",
        "#!/bin/bash\n[[ \"${1:-}\" == --help ]] && "
        "printf '%s\\n' '  --ask-for-approval <POLICY>'\n",
    )
    _executable(bindir / "claude", "#!/bin/bash\nexit 0\n")

    handoff = tmp_path / "child-token"
    handoff.write_text("child-owner-token", encoding="utf-8")
    handoff.chmod(0o600)

    env = os.environ.copy()
    env.update({
        "PATH": f"{bindir}:{env['PATH']}",
        "HOME": str(tmp_path / "home"),
        "PARENT_AGENT": "ParentAgent",
        "PROJECT_KEY": "/shared/project",
        "AGENTSTACK_PROJECT_KEY": "/shared/project",
        "AGENTSTACK_RUNTIME_DIR": str(runtime),
        "AGENTSTACK_HOOKS_DIR": str(ROOT / "hooks"),
        "AGENTSTACK_HOME": str(tmp_path / "agentstack"),
        "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
        "AGENTSTACK_REGISTER_LIB": str(ROOT / "bin" / "lib" / "agentstack-register.sh"),
        "AGENTSTACK_MCP_PROXY": str(tmp_path / "missing-proxy"),
        "AGENTSTACK_TERMINAL": "none",
        "FAKE_TMUX_LOG": str(tmux_log),
        "FAKE_TMUX_ALIVE": str(tmux_alive),
        "FAKE_TRUST_ACCEPTED": str(trust_accepted),
    })
    pathlib.Path(env["HOME"]).mkdir()

    result = subprocess.run(
        [
            "/bin/bash", str(SPAWN),
            "--pre-registered", "Fresh-Curie",
            "--child-token-file", str(handoff),
            "--model", "sonnet",
            "read the inbox", str(workdir),
        ],
        cwd=ROOT, env=env, text=True,
        # Generous: this only guards against a hang. Whether the launcher
        # waited out its readiness timeout is measured in polls below, since
        # `sleep` is faked and wall time only reflects machine load.
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Fresh-Curie"
    assert "Claude trust dialog detected" in result.stderr
    calls = tmux_log.read_text(encoding="utf-8").splitlines()
    assert sum(call.endswith(" C-m") for call in calls) >= 2
    # Waiting out the 60s timeout would take 30 polls of 2s; accepting the
    # trust dialog and seeing the prompt takes about five (deterministic:
    # sleep is faked), so 10 leaves room without hiding a wait-out.
    polls = [call for call in calls if call.startswith("capture-pane") and " -S " not in f" {call} "]
    assert 1 <= len(polls) <= 10, calls
