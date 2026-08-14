"""Regression coverage for pre-registered embedded task launches."""
from __future__ import annotations

import os
import pathlib
import stat
import subprocess

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPAWN = ROOT / "hooks" / "spawn_child.sh"


def _executable(path: pathlib.Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _fake_launch_env(
    tmp_path: pathlib.Path, *, codex: bool,
) -> tuple[dict[str, str], pathlib.Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tmux_log = tmp_path / "tmux.log"
    tmux_alive = tmp_path / "tmux.alive"

    _executable(
        bindir / "tmux",
        "#!/bin/bash\n"
        "{ printf 'CALL'; for arg in \"$@\"; do printf '\\034%s' \"$arg\"; done; "
        "printf '\\035\\n'; } >> \"$FAKE_TMUX_LOG\"\n"
        "case \"${1:-}\" in\n"
        "  new-session) : > \"$FAKE_TMUX_ALIVE\" ;;\n"
        "  capture-pane)\n"
        "    if [[ \"${FAKE_CODEX:-0}\" == 1 ]]; then\n"
        "      printf '\\ngpt-5.5 xhigh · ~/workspace\\n'\n"
        "    else\n"
        "      printf '\\n❯ \\n'\n"
        "    fi ;;\n"
        "  has-session) [[ -f \"$FAKE_TMUX_ALIVE\" ]] ;;\n"
        "  kill-session) rm -f \"$FAKE_TMUX_ALIVE\" ;;\n"
        "  display-message) printf 'ParentAgent\\n' ;;\n"
        "esac\n",
    )
    _executable(bindir / "sleep", "#!/bin/bash\nexit 0\n")
    _executable(
        bindir / "codex",
        "#!/bin/bash\n"
        "if [[ \"${1:-}\" == --help ]]; then\n"
        "  printf '%s\\n' '  --ask-for-approval <POLICY>'\n"
        "fi\n",
    )
    _executable(bindir / "claude", "#!/bin/bash\nexit 0\n")

    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "runtime"
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    env = os.environ.copy()
    env.update({
        "PATH": f"{bindir}:{env['PATH']}",
        "HOME": str(home),
        "PARENT_AGENT": "ParentAgent",
        "PROJECT_KEY": "/shared/project",
        "AGENTSTACK_PROJECT_KEY": "/shared/project",
        "AGENTSTACK_RUNTIME_DIR": str(runtime),
        "AGENTSTACK_HOOKS_DIR": str(ROOT / "hooks"),
        "AGENTSTACK_HOME": str(tmp_path / "agentstack"),
        "AGENTSTACK_REGISTER_LIB": str(
            ROOT / "bin" / "lib" / "agentstack-register.sh"
        ),
        "AGENTSTACK_MCP_PROXY": str(tmp_path / "missing-proxy"),
        "AGENTSTACK_TERMINAL": "none",
        "FAKE_TMUX_LOG": str(tmux_log),
        "FAKE_TMUX_ALIVE": str(tmux_alive),
        "FAKE_CODEX": "1" if codex else "0",
    })
    return env, workdir


def test_embed_task_requires_pre_registered(tmp_path: pathlib.Path) -> None:
    result = subprocess.run(
        ["/bin/bash", str(SPAWN), "--embed-task", "--unsafe-no-resources", "task"],
        cwd=ROOT,
        env={**os.environ, "PROJECT_KEY": "/shared/project"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert "Error: --embed-task requires --pre-registered" in result.stderr


def test_unreadable_task_file_fails_clearly(tmp_path: pathlib.Path) -> None:
    missing = tmp_path / "missing-task.md"
    result = subprocess.run(
        [
            "/bin/bash", str(SPAWN), "--pre-registered", "EmbedClaude",
            "--embed-task", "--task-file", str(missing),
        ],
        cwd=ROOT,
        env={**os.environ, "PROJECT_KEY": "/shared/project"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert f"Error: --task-file not readable: {missing}" in result.stderr


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
def test_task_file_is_embedded_literally_for_both_launch_paths(
    tmp_path: pathlib.Path, codex: bool,
) -> None:
    env, workdir = _fake_launch_env(tmp_path, codex=codex)
    task_file = tmp_path / "task.md"
    backtick_marker = tmp_path / "backtick-expanded"
    dollar_marker = tmp_path / "dollar-expanded"
    task = (
        "Read `literal-code` and do not execute "
        f"`touch {backtick_marker}` or $(touch {dollar_marker}).\n"
        "Second task line stays literal."
    )
    task_file.write_text(task, encoding="utf-8")
    handoff = tmp_path / "child-token"
    handoff.write_text("child-owner-token", encoding="utf-8")
    handoff.chmod(0o600)
    child_name = "EmbedCodex" if codex else "EmbedClaude"

    args = [
        "/bin/bash", str(SPAWN),
        "--pre-registered", child_name,
        "--child-token-file", str(handoff),
        "--embed-task", "--task-file", str(task_file),
    ]
    if codex:
        args.append("--codex")
        # A positional TASK remains accepted for compatibility but loses to the
        # file; the second positional keeps its existing workdir meaning.
        args.extend(["IGNORED POSITIONAL TASK", str(workdir)])
    else:
        # The recommended file-only form treats its sole positional as workdir.
        args.append(str(workdir))

    result = subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == child_name
    injected = pathlib.Path(env["FAKE_TMUX_LOG"]).read_text(encoding="utf-8")
    assert task in injected
    assert "IGNORED POSITIONAL TASK" not in injected
    assert "登録は親が完了済み・儀式不要です" in injected
    assert "ensure_project・register_agent・fetch_inbox は実行しないでください" in injected
    assert f"あなたは {child_name}（親: ParentAgent）" in injected
    assert "現在時刻:" in injected
    assert "project_key は /shared/project" in injected
    assert "send_message で ParentAgent に報告してください" in injected
    assert "launch prompt is canonical; do not send task mail" in result.stderr
    assert not backtick_marker.exists()
    assert not dollar_marker.exists()
