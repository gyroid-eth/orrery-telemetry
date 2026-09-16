"""Regression coverage for pre-registered embedded task launches."""
from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from service_teardown import TEST_LABEL_PREFIX  # noqa: E402


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
        "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
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


def _codex_handoff(tmp_path: pathlib.Path, child_name: str) -> pathlib.Path:
    handoff = tmp_path / "child-token"
    handoff.write_text("child-owner-token", encoding="utf-8")
    handoff.chmod(0o600)
    binding = handoff.with_name(handoff.name + ".binding.json")
    binding.write_text(
        json.dumps({
            "agent_id": 73,
            "agent_name": child_name,
            "project_key": "/shared/project",
            "program": "codex",
        }),
        encoding="utf-8",
    )
    binding.chmod(0o600)
    return handoff


def _enable_fake_codex_profile(
    tmp_path: pathlib.Path, env: dict[str, str],
) -> None:
    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    (source_home / "auth.json").write_text("{}\n", encoding="utf-8")
    (source_home / "config.toml").write_text(
        '[mcp_servers.chrome-devtools]\ncommand = "npx"\n',
        encoding="utf-8",
    )
    runner = tmp_path / "run-mcp.sh"
    _executable(runner, "#!/bin/bash\nexit 0\n")
    env["CODEX_HOME"] = str(source_home)
    env["AGENTSTACK_MCP_PROXY"] = str(runner)


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


def test_codex_mcp_profile_rejects_unknown_value() -> None:
    result = subprocess.run(
        [
            "/bin/bash", str(SPAWN), "--codex", "--codex-mcp", "everything",
            "--unsafe-no-resources", "task",
        ],
        cwd=ROOT,
        env={**os.environ, "PROJECT_KEY": "/shared/project"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert "--codex-mcp must be inherit or orrery-only" in result.stderr


def test_codex_mcp_profile_rejects_claude_child() -> None:
    result = subprocess.run(
        [
            "/bin/bash", str(SPAWN), "--codex-mcp", "orrery-only",
            "--unsafe-no-resources", "task",
        ],
        cwd=ROOT,
        env={**os.environ, "PROJECT_KEY": "/shared/project"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert "--codex-mcp is only valid with --codex" in result.stderr


def test_orrery_only_stops_before_cli_when_profile_home_cannot_be_built(
    tmp_path: pathlib.Path,
) -> None:
    env, workdir = _fake_launch_env(tmp_path, codex=True)
    child_name = "ProfileFailure"
    handoff = _codex_handoff(tmp_path, child_name)
    result = subprocess.run(
        [
            "/bin/bash", str(SPAWN),
            "--pre-registered", child_name,
            "--child-token-file", str(handoff),
            "--codex", "--codex-mcp", "orrery-only",
            "task", str(workdir),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "could not create the requested Codex MCP profile" in result.stderr
    tmux_log = pathlib.Path(env["FAKE_TMUX_LOG"])
    assert not tmux_log.exists() or "new-session" not in tmux_log.read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("prompt_path", ["embed", "mail", "standalone"])
def test_orrery_only_notice_reaches_each_preregistered_codex_prompt(
    tmp_path: pathlib.Path, prompt_path: str,
) -> None:
    env, workdir = _fake_launch_env(tmp_path, codex=True)
    _enable_fake_codex_profile(tmp_path, env)
    child_name = f"Profile-{prompt_path}"
    handoff = _codex_handoff(tmp_path, child_name)
    args = [
        "/bin/bash", str(SPAWN),
        "--pre-registered", child_name,
        "--child-token-file", str(handoff),
        "--codex", "--codex-mcp", "orrery-only",
    ]
    if prompt_path == "embed":
        task_file = tmp_path / "task.md"
        task_file.write_text("embedded task", encoding="utf-8")
        args.extend(["--embed-task", "--task-file", str(task_file), str(workdir)])
    elif prompt_path == "standalone":
        args.extend(["--standalone", "standalone task", str(workdir)])
    else:
        args.extend(["mail task", str(workdir)])

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
    injected = pathlib.Path(env["FAKE_TMUX_LOG"]).read_text(encoding="utf-8")
    assert "shell/files and authenticated ORRERY Mail remain available" in injected
    assert "Other inherited MCP servers and plugins are disabled" in injected
    assert "existing AgentStack session-binding plugin configuration is preserved" in injected
    assert "If a required tool is unavailable, ask your parent agent for help (or the operator in standalone mode)." in injected
    if prompt_path == "mail":
        assert "Use only the first matching coordination route" in injected
        assert "provided tool descriptions and argument schema show a bound ORRERY proxy" in injected
        assert "call fetch_inbox with only the arguments accepted by that schema" in injected
        assert "actual provided schema is confirmed raw/direct" in injected
        assert "A proxy failure is not evidence to switch to raw or a helper" in injected
        assert "treat the inbox request as authoritative" in injected
        assert "First, if" not in injected
        assert "agentstack-reregister" not in injected


def test_both_codex_launch_paths_append_the_profile_notice() -> None:
    text = SPAWN.read_text(encoding="utf-8")
    assert text.count(
        'CODEX_PROMPT="$(append_codex_mcp_profile_notice "$CODEX_PROMPT")"'
    ) == 2


def test_both_mail_task_entrypoints_use_the_route_aware_prompt_builder() -> None:
    text = SPAWN.read_text(encoding="utf-8")
    assert text.count(
        'CODEX_PROMPT="$(build_codex_mail_task_prompt "$CHILD_NAME" "$PARENT_NAME")"'
    ) == 2
    assert "The canonical task is in your ORRERY Mail inbox." in text
    assert "A proxy failure is not evidence to switch to raw or a helper." in text
    assert "First, if ${REREGISTER_HELPER" not in text


def test_generated_task_mail_uses_connection_specific_project_and_renewal() -> None:
    text = SPAWN.read_text(encoding="utf-8")
    start = text.index('BODY_MD="## Task')
    end = text.index("\n\nSEND_ARGS=", start)
    assignment = text[start:end]
    result = subprocess.run(
        ["/bin/bash", "-c", assignment + '\nprintf \'%s\' "$BODY_MD"\n'],
        cwd=ROOT,
        env={
            **os.environ,
            "TASK": "fixture task",
            "PARENT_NAME": "ParentAgent",
            "WORK_DIR": "/fixture/worktree",
            "RESOURCE_NOTE": "\n- Reserved resources: src/example.py",
            "WORKTREE_NOTE": "",
            "PROJECT_KEY": "/fixture/canonical-project",
            "RESOURCE_TTL": "600",
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    body = result.stdout
    assert "`/fixture/canonical-project` is the canonical ORRERY Mail project_key" in body
    assert "raw/direct MCP, use this value where the actual schema accepts" in body
    assert "bound proxy, do not add caller identity or project fields" in body
    assert "Do not acquire the same paths again" in body
    assert "bound proxy uses `renew_reservations`" in body
    assert "raw/direct MCP uses `renew_file_reservations`" in body
    assert "prefer renew_file_reservations" not in body


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
    if codex:
        binding = handoff.with_name(handoff.name + ".binding.json")
        binding.write_text(
            json.dumps({
                "agent_id": 73,
                "agent_name": child_name,
                "project_key": "/shared/project",
                "program": "codex",
            }),
            encoding="utf-8",
        )
        binding.chmod(0o600)

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
    assert "Capability notice:" not in injected
    assert "launch prompt is canonical; do not send task mail" in result.stderr
    assert not backtick_marker.exists()
    assert not dollar_marker.exists()
