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
        # A Codex child reads its task from the 0600 file named in its env
        # (the argv path); record what it would have received.
        "  new-session) : > \"$FAKE_TMUX_ALIVE\"\n"
        "    for arg in \"$@\"; do case \"$arg\" in AGENTSTACK_CODEX_PROMPT_FILE=*)\n"
        "      printf 'ARGV_TASK\\034' >> \"$FAKE_TMUX_LOG\"; cat \"${arg#*=}\" >> \"$FAKE_TMUX_LOG\"\n"
        # Codex then shows the task it was started with, as the real one does.
        "      { printf '› '; cat \"${arg#*=}\"; printf '\\n'; } > \"$FAKE_TMUX_LOG.screen\" ;;\n"
        # A Claude child reads its first prompt from the file named in its env
        # (the launch argument); record what it would have received and its mode.
        "      CLAUDE_CHILD_PROMPT_FILE=*) printf 'ARGV_TASK\\034%s\\034' \"$(stat -c %a \"${arg#*=}\" 2>/dev/null || stat -f %Lp \"${arg#*=}\")\" >> \"$FAKE_TMUX_LOG\"\n"
        "      cat \"${arg#*=}\" >> \"$FAKE_TMUX_LOG\" ;; esac; done ;;\n"
        "  capture-pane)\n"
        "    if [[ \"${FAKE_CODEX:-0}\" == 1 ]]; then\n"
        "      [[ -f \"$FAKE_TMUX_LOG.screen\" ]] && cat \"$FAKE_TMUX_LOG.screen\"\n"
        "      printf '\\ngpt-5.5 xhigh · ~/workspace\\n'\n"
        "    else\n"
        "      printf '\\n❯ \\n'\n"
        "    fi ;;\n"
        "  has-session) [[ -f \"$FAKE_TMUX_ALIVE\" ]] ;;\n"
        "  kill-session) rm -f \"$FAKE_TMUX_ALIVE\" ;;\n"
        "  load-buffer) cat \"$4\" >> \"$FAKE_TMUX_LOG\" ;;\n"
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
        # A relaunched retired child is unretired through Mail; never let a
        # fixture reach a Mail that happens to run on the default port.
        "AGENTSTACK_MCP_URL": "http://127.0.0.1:9/mcp",
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
        # Without a history-binding receipt the launcher's watch runs all its
        # polls (each starts the status helper); allow for a loaded machine.
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    injected = pathlib.Path(env["FAKE_TMUX_LOG"]).read_text(encoding="utf-8")
    # The task went in as the child's argv, never through a paste buffer. No
    # history-binding receipt exists here, so its start is reported as unknown.
    assert "ARGV_TASK" in injected
    assert "first-task confirmation unknown" in result.stderr
    assert "\034load-buffer" not in injected and "\034paste-buffer" not in injected
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
    binding = handoff.with_name(handoff.name + ".binding.json")
    binding.write_text(
        json.dumps({
            "agent_id": 73, "agent_name": child_name,
            "project_key": "/shared/project", "program": "codex" if codex else "claude-code",
        }), encoding="utf-8",
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
        # Without a history-binding receipt the launcher's watch runs all its
        # polls (each starts the status helper); allow for a loaded machine.
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == child_name
    injected = pathlib.Path(env["FAKE_TMUX_LOG"]).read_text(encoding="utf-8")
    assert task in injected
    # Both get the task as their argv, never as a paste: a pasted task reaches
    # a Claude child as <pasted_content>, which it may decline to act on.
    assert "ARGV_TASK" in injected
    assert "\034paste-buffer" not in injected
    if codex:
        assert "first-task confirmation unknown" in result.stderr
    else:
        assert "ARGV_TASK\034600\034" in injected, "the first prompt file must be private"
    assert "IGNORED POSITIONAL TASK" not in injected
    assert "登録は親が完了済み・儀式不要です" in injected
    assert "ensure_project・register_agent・fetch_inbox は実行しないでください" in injected
    assert f"あなたは {child_name}（親: ParentAgent）" in injected
    assert "現在時刻:" in injected
    assert "project_key は /shared/project" in injected
    assert "ORRERY Mail の send_message（Claude Code の SendMessage ではない）で ParentAgent に報告してください" in injected
    assert "Capability notice:" not in injected
    assert "launch prompt is canonical; do not send task mail" in result.stderr
    assert not backtick_marker.exists()
    assert not dollar_marker.exists()


@pytest.mark.parametrize('codex', [False, True], ids=['claude', 'codex'])
@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'wrong_provider', 'wrong_name', 'wrong_project', 'unsafe'])
def test_invalid_handoff_preserves_original_registration_before_tmux(tmp_path, codex, damage):
    env, workdir = _fake_launch_env(tmp_path, codex=codex)
    env['AGENTSTACK_CODEX_BIN'] = str(tmp_path / 'bin' / 'codex')
    name = 'PreservedOwner'
    handoff = _codex_handoff(tmp_path, name)
    binding = handoff.with_name(handoff.name + '.binding.json')
    data = json.loads(binding.read_text())
    data['program'] = 'codex' if codex else 'claude-code'
    if damage == 'wrong_provider':
        data['program'] = 'claude-code' if codex else 'codex'
    elif damage == 'wrong_name':
        data['agent_name'] = 'AnotherOwner'
    elif damage == 'wrong_project':
        data['project_key'] = '/other/project'
    binding.write_text(json.dumps(data))
    if damage == 'missing':
        binding.unlink()
    elif damage == 'corrupt':
        binding.write_text('{broken')
    elif damage == 'unsafe':
        binding.chmod(0o644)
    runtime = pathlib.Path(env['AGENTSTACK_RUNTIME_DIR'])
    canonical = runtime / ('agent_token_' + name)
    state = runtime / 'child-agents' / (name + '.json')
    canonical.parent.mkdir(parents=True, exist_ok=True)
    state.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text('original-owner-token')
    canonical.chmod(0o600)
    state.write_text(json.dumps({'agent_id': 73, 'agent_name': name, 'project_key': '/shared/project',
                                'program': 'codex' if codex else 'claude-code',
                                'registration_token': 'original-owner-token'}))
    state.chmod(0o600)
    managed = runtime / 'managed_agents.txt'
    managed.write_text(name + '\nAnotherOwner\n')
    originals = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in (canonical, state, handoff, managed)}
    args = ['/bin/bash', str(SPAWN), '--pre-registered', name, '--child-token-file', str(handoff)]
    if codex:
        args.append('--codex')
    result = subprocess.run([*args, 'fixture task', str(workdir)], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0, result
    log = pathlib.Path(env['FAKE_TMUX_LOG'])
    assert not log.exists() or 'new-session' not in log.read_text()
    for path, before in originals.items():
        assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode) == before
    assert not list(state.parent.glob('*.registration-pending.json'))


@pytest.mark.parametrize('codex', [False, True], ids=['claude', 'codex'])
def test_failed_startup_restores_existing_registration_and_keeps_handoff(tmp_path, codex):
    from hooks import child_resume
    env, workdir = _fake_launch_env(tmp_path, codex=codex)
    env['AGENTSTACK_CODEX_BIN'] = str(tmp_path / 'bin' / 'codex')
    name = 'FailedStartup'
    handoff = _codex_handoff(tmp_path, name)
    binding = handoff.with_name(handoff.name + '.binding.json')
    data = json.loads(binding.read_text())
    data['program'] = 'codex' if codex else 'claude-code'
    binding.write_text(json.dumps(data))
    runtime = pathlib.Path(env['AGENTSTACK_RUNTIME_DIR'])
    canonical = runtime / ('agent_token_' + name)
    state = runtime / 'child-agents' / (name + '.json')
    canonical.parent.mkdir(parents=True, exist_ok=True)
    state.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text('original-owner-token')
    canonical.chmod(0o600)
    state.write_text(json.dumps({**data, 'registration_token': 'original-owner-token'}))
    state.chmod(0o600)
    child_resume.prepare_active_state(runtime, name, project_key='/shared/project', mcp_profile='inherit')
    assert child_resume.mark_retired(runtime, name, retention_days=30)
    originals = {p: (p.read_bytes(), p.stat().st_mode) for p in (canonical, state, handoff, binding)}
    # Startup refuses before a real CLI can run. The child is retired, and the
    # fixture's Mail is a closed port, so both stop before tmux when it cannot
    # be made active again; the tmux fixture below would reject Claude anyway.
    if not codex:
        _executable(tmp_path / 'bin' / 'tmux', '#!/bin/bash\n[[ "$1" != new-session ]] || exit 1\nexit 0\n')
    args = ['/bin/bash', str(SPAWN), '--pre-registered', name, '--child-token-file', str(handoff)]
    if codex:
        args.extend(['--codex', '--codex-mcp', 'orrery-only'])
    result = subprocess.run([*args, 'fixture task', str(workdir)], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0, result
    for path, before in originals.items():
        assert (path.read_bytes(), path.stat().st_mode) == before
    assert not list(state.parent.glob('.*.registration-pending.json'))


@pytest.mark.parametrize('program', ['claude', 'claude-code', 'codex', 'codex-cli'])
def test_valid_registration_staging_commits_the_expected_provider(tmp_path, program):
    from hooks import child_resume
    name = 'VerifiedProvider'
    handoff = _codex_handoff(tmp_path, name)
    binding = handoff.with_name(handoff.name + '.binding.json')
    data = json.loads(binding.read_text())
    data['program'] = program
    binding.write_text(json.dumps(data))
    runtime = tmp_path / 'runtime'
    token = child_resume.stage_registration(runtime, name, project_key='/shared/project', program=program, generation='a' * 32,
                                           source=handoff, binding=binding)
    state = child_resume.prepare_active_state(runtime, name, project_key='/shared/project', program=program, generation='a' * 32)
    child_resume.finish_registration(runtime, name, generation='a' * 32, rollback=False)
    assert token.read_text() == 'child-owner-token'
    assert state['program'] == program and state['provider'] == ('codex' if program.startswith('codex') else 'claude')
    assert handoff.exists() and binding.exists()  # Consumption belongs to the successful launcher.
    assert token.stat().st_mode & 0o777 == 0o600
    assert not list((runtime / 'child-agents').glob('.*.registration-pending.json'))


@pytest.mark.parametrize('program', ['claude-code', 'codex'])
@pytest.mark.parametrize('rollback', [False, True], ids=['late-commit', 'late-rollback'])
def test_purged_attempt_cannot_finalize_or_prepare_the_same_identity_retry(tmp_path, program, rollback):
    from hooks import child_resume
    runtime = tmp_path / 'runtime'
    name = 'GenerationOwner'
    handoff = _codex_handoff(tmp_path, name)
    binding = handoff.with_name(handoff.name + '.binding.json')
    data = json.loads(binding.read_text())
    data['program'] = program
    binding.write_text(json.dumps(data))
    old, new = 'a' * 32, 'b' * 32
    def stage(generation):
        child_resume.stage_registration(runtime, name, project_key='/shared/project', program=program,
                                        generation=generation, source=handoff, binding=binding)
        child_resume.prepare_active_state(runtime, name, project_key='/shared/project', program=program,
                                          generation=generation)
    stage(old)
    assert child_resume.purge_one(runtime, name, reason='purged')
    stage(new)  # Same name, numeric ID, owner token and provider; only the attempt differs.
    token = runtime / ('agent_token_' + name)
    state = runtime / 'child-agents' / (name + '.json')
    pending = state.with_name('.' + name + '.registration-pending.json')
    tombstone = runtime / 'child-resume-tombstones' / '73.json'
    before = {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in (token, state, pending, handoff, binding)}
    assert not child_resume.finish_registration(runtime, name, generation=old, rollback=rollback)
    with pytest.raises(child_resume.ResumeStateError, match='attempt has changed'):
        child_resume.prepare_active_state(runtime, name, project_key='/shared/project', program=program, generation=old)
    for path, saved in before.items():
        assert (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) == saved
    assert json.loads(pending.read_text())['generation'] == new
    # Positive control: the retry can finish its own transaction.
    assert child_resume.finish_registration(runtime, name, generation=new, rollback=rollback)
    assert not pending.exists()
    if rollback:
        assert not token.exists() and not state.exists() and tombstone.exists()
    else:
        assert token.read_text() == 'child-owner-token' and state.exists() and not tombstone.exists()


def test_late_launcher_exit_skips_the_new_attempt_session_registry_and_worktree(tmp_path):
    from hooks import child_resume
    env, _workdir = _fake_launch_env(tmp_path, codex=False)
    runtime = pathlib.Path(env['AGENTSTACK_RUNTIME_DIR'])
    name = 'GenerationOwner'
    handoff = _codex_handoff(tmp_path, name)
    binding = handoff.with_name(handoff.name + '.binding.json')
    data = json.loads(binding.read_text())
    data['program'] = 'claude-code'
    binding.write_text(json.dumps(data))
    for generation in ('a' * 32, 'b' * 32):
        child_resume.stage_registration(runtime, name, project_key='/shared/project', program='claude-code',
                                        generation=generation, source=handoff, binding=binding)
        child_resume.prepare_active_state(runtime, name, project_key='/shared/project', program='claude-code', generation=generation)
        if generation == 'a' * 32:
            assert child_resume.purge_one(runtime, name, reason='purged')
    managed = runtime / 'managed_agents.txt'
    managed.write_text(name + '\nOtherOwner\n')
    before = {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in runtime.rglob('*') if p.is_file()}
    spawn = SPAWN.read_text()
    wrappers = spawn[spawn.index('stage_child_registration() {'):spawn.index('# Verify that a Codex token')]
    cleanup = spawn[spawn.index('    cleanup_preregister_failure() {'):spawn.index('    trap cleanup_preregister_failure EXIT')]
    marker = tmp_path / 'unwanted-shared-cleanup'
    script = ('set -eu\n' +
              'RUNTIME_DIR=' + str(runtime) + '\nHOOKS_DIR=' + str(ROOT / 'hooks') + '\n' +
              'CHILD_NAME=' + name + '\nCHILD_REGISTRATION_GENERATION=' + 'a' * 32 + '\n' +
              'PRE_REGISTERED_SUCCESS=false\nPRE_REGISTERED_ADOPTION_PENDING=true\n' +
              'PRE_REGISTERED_SESSION_STARTED=true\nPRE_REGISTERED_MANAGED_ADDED=true\n' +
              'MANAGED_FILE=' + str(managed) + '\n' +
              'warn_if_uninjected() { :; }\n' +
              'discard_claude_launch_record() { touch ' + str(marker) + '; }\n' +
              'cleanup_worktree() { touch ' + str(marker) + '; }\n' +
              wrappers + cleanup + '\ncleanup_preregister_failure\n')
    result = subprocess.run(['/bin/bash', '-c', script], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'nothing changed' in result.stderr
    assert not marker.exists() and not pathlib.Path(env['FAKE_TMUX_LOG']).exists()
    for path, saved in before.items():
        assert (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) == saved
