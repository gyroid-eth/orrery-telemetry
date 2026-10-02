"""Claude in Chrome for Claude children (--claude-chrome).

The default is "inherit": with no request the child's launch command is the
one used before the option existed, and the user's own Claude settings decide
whether Chrome is available. A request adds --chrome on every launch path and
records what a resume must restore.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_spawn_child_embed_task import (  # noqa: E402
    _codex_handoff, _executable, _fake_launch_env,
)

import dashboard.server as server  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPAWN = ROOT / "hooks" / "spawn_child.sh"
POLICY = ROOT / "hooks" / "claude_chrome_policy.py"
REMINDER = ROOT / "hooks" / "session-start-reminder.sh"

# The Claude child's inner launch script before --claude-chrome existed
# (hooks/spawn_child.sh at 0a43a3c, both launch sites), except that the
# binary is now the path the launcher resolved (CLAUDE_CHILD_BIN), bare
# `claude` when it found none.
PRE_CHROME_INNER = (
    'export PATH="$HOME/.local/bin:$PATH"; MCP_ARGS=(); '
    '[[ -n "$CLAUDE_CHILD_MCP_CONFIG" ]] && MCP_ARGS=(--mcp-config '
    '"$CLAUDE_CHILD_MCP_CONFIG" --strict-mcp-config); '
    '"${CLAUDE_CHILD_BIN:-claude}" --model "$CLAUDE_CHILD_MODEL" "${MCP_ARGS[@]}"; '
    '/bin/bash "$AGENTSTACK_HOOKS_DIR/cleanup-child-agent.sh"'
)
CHROME_INNER = PRE_CHROME_INNER.replace(
    '"${MCP_ARGS[@]}";', '"${MCP_ARGS[@]}" --chrome;'
)
# A launched child also takes its first prompt as the `claude [prompt]`
# argument, read once from a private file (see claude_child_launch_command).
ARGV_PROMPT = (' --append-system-prompt "$CLAUDE_CHILD_SYSTEM_PROMPT"'
               ' "$(cat "$CLAUDE_CHILD_PROMPT_FILE"; rm -f "$CLAUDE_CHILD_PROMPT_FILE")"')
LAUNCHED_CHROME_INNER = CHROME_INNER.replace("--chrome;", "--chrome" + ARGV_PROMPT + ";")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _hooks_with_warm_pool(tmp_path: pathlib.Path) -> pathlib.Path:
    """A hooks dir identical to the repo's plus a warm pool that is always ready.

    No warm_pool.sh ships today; the fake proves that a Chrome request skips
    the claim itself, not merely that no pool exists."""
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for entry in (ROOT / "hooks").iterdir():
        (hooks / entry.name).symlink_to(entry)
    _executable(
        hooks / "warm_pool.sh",
        "#!/bin/bash\n"
        "case \"$1\" in\n"
        "  status) printf '%b\\n' \"${FAKE_WARM_STATUS:-opus ready (claude-opus-5-5)\\nsonnet ready (claude-sonnet-5-5)}\" ;;\n"
        "  claim) printf 'claim %s\\n' \"$3\" >> \"$FAKE_WARM_LOG\"; "
        "tmux new-session -d -s \"$3\" warm; printf '%s\\n' \"$3\" ;;\n"
        "esac\n",
    )
    return hooks


def _handoff(tmp_path: pathlib.Path, name="SameChild") -> pathlib.Path:
    handoff = tmp_path / f"child-token-{len(list(tmp_path.glob('child-token-*')))}"
    handoff.write_text("child-owner-token", encoding="utf-8")
    handoff.chmod(0o600)
    binding = handoff.with_name(handoff.name + ".binding.json")
    binding.write_text(json.dumps({"agent_id": 73, "agent_name": name,
                                   "project_key": "/shared/project", "program": "claude-code"}), encoding="utf-8")
    binding.chmod(0o600)
    return handoff


def _spawn(tmp_path, env, workdir, name, *flags, task="mail task", codex=False):
    handoff = _codex_handoff(tmp_path, name) if codex else _handoff(tmp_path, name)
    args = [
        "/bin/bash", str(SPAWN), "--pre-registered", name,
        "--child-token-file", str(handoff),
    ]
    if codex:
        args.append("--codex")
    args.extend(flags)
    args.extend([task, str(workdir)])
    return subprocess.run(
        args, cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False,
    )


def _calls(env) -> list[list[str]]:
    log = pathlib.Path(env["FAKE_TMUX_LOG"])
    if not log.exists():
        return []
    calls = []
    for chunk in log.read_text(encoding="utf-8").split("CALL")[1:]:
        body = chunk.split("\035", 1)[0]
        calls.append(body.split("\034")[1:])
    return calls


def _new_session(env) -> list[str]:
    sessions = [c for c in _calls(env) if c and c[0] == "new-session"]
    assert len(sessions) == 1, sessions
    return sessions[0]


def _log_text(env) -> str:
    return pathlib.Path(env["FAKE_TMUX_LOG"]).read_text(encoding="utf-8")


def _launch_env(tmp_path, *, codex=False):
    env, workdir = _fake_launch_env(tmp_path, codex=codex)
    for key in ("AGENTSTACK_CLAUDE_CHILD_CHROME", "AGENTSTACK_CLAUDE_CHILD_CHROME_DEVICE"):
        env.pop(key, None)
    env["FAKE_WARM_LOG"] = str(tmp_path / "warm.log")
    # The aliases (and so the warm pool's expected model) come from the bundled
    # table, not from whatever the real ~/.claude catalog names today.
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "no-claude-config")
    return env, workdir


def _state_dir(env) -> pathlib.Path:
    return pathlib.Path(env["AGENTSTACK_RUNTIME_DIR"]) / "child-agents"


def _records(env, name) -> list[pathlib.Path]:
    return sorted(_state_dir(env).glob(f"{name}.claude-launch.*.json"))


def _launch_id(env) -> str:
    args = _new_session(env)
    ids = [a.split("=", 1)[1] for a in args if a.startswith("AGENTSTACK_CLAUDE_LAUNCH_ID=")]
    assert len(ids) == 1, args
    return ids[0]


# --------------------------------------------------------------------------- #
# launch command
# --------------------------------------------------------------------------- #
def _builder_output(chrome: bool) -> str:
    text = SPAWN.read_text(encoding="utf-8")
    start = text.index("claude_child_launch_command() {")
    end = text.index("\n}\n", start) + 3
    script = (
        text[start:end]
        + f'CHILD_SHELL=/bin/bash; CLAUDE_CHILD_CHROME={"true" if chrome else "false"}\n'
        + "claude_child_launch_command\n"
    )
    return subprocess.run(
        ["/bin/bash", "-c", script], text=True, capture_output=True, check=True
    ).stdout


def test_builder_is_byte_identical_to_the_pre_chrome_command_when_not_requested():
    # Covers the legacy launch site too: both sites call this one builder.
    assert _builder_output(False) == f"/bin/bash -lc '{PRE_CHROME_INNER}'"


def test_builder_adds_only_chrome_when_requested():
    assert _builder_output(True) == f"/bin/bash -lc '{CHROME_INNER}'"


def test_default_spawn_matches_the_pre_chrome_launcher_exactly(tmp_path):
    """Run the launcher from before this change and this one on the same input."""
    old_hooks = tmp_path / "old" / "hooks"
    old_hooks.mkdir(parents=True)
    (tmp_path / "old" / "dashboard").symlink_to(ROOT / "dashboard")
    old_text = subprocess.run(
        ["git", "-C", str(ROOT), "show", "0a43a3c:hooks/spawn_child.sh"],
        text=True, capture_output=True, check=False,
    )
    if old_text.returncode != 0:
        pytest.skip("pre-change launcher is not in this checkout's history")
    (old_hooks / "spawn_child.sh").write_text(old_text.stdout, encoding="utf-8")

    launches, prompts = {}, {}
    for label, script in (("old", old_hooks / "spawn_child.sh"), ("new", SPAWN)):
        (tmp_path / label).mkdir(exist_ok=True)
        env, workdir = _launch_env(tmp_path / label)
        result = subprocess.run(
            ["/bin/bash", str(script), "--pre-registered", "SameChild",
             "--child-token-file", str(_handoff(tmp_path / label)),
             "mail task", str(workdir)],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=60, check=False,
        )
        assert result.returncode == 0, result.stderr
        log = _log_text(env)
        launch = [arg.replace(str(tmp_path / label), "<tmp>") for arg in _new_session(env)]
        if label == "old":
            # The old launcher pasted the prompt: it is the load-buffer content.
            _, _, rest = log.partition("\034load-buffer\034")
            prompts[label] = rest.split("\035\n", 1)[1].split("CALL", 1)[0]
        else:
            raw_inner = launch[-1]
            # The new one passes the same text as the launch argument instead,
            # and runs the claude path it resolved (bare `claude` if none).
            for variable in ("CLAUDE_CHILD_PROMPT_FILE=", "CLAUDE_CHILD_SYSTEM_PROMPT=", "CLAUDE_CHILD_BIN="):
                index = next(i for i, arg in enumerate(launch) if arg.startswith(variable)) - 1
                assert launch[index] == "-e"
                del launch[index:index + 2]
            launch[-1] = launch[-1].replace(ARGV_PROMPT, "").replace('"${CLAUDE_CHILD_BIN:-claude}"', "claude")
            prompts[label] = log.split("ARGV_TASK\034600\034", 1)[1].split("CALL", 1)[0]
            assert "\034load-buffer\034" not in log and "\034paste-buffer\034" not in log
        launches[label] = launch
    # Without --claude-chrome the child is launched exactly as before, and gets
    # exactly the prompt it got before; only how the prompt travels changed.
    assert launches["new"] == launches["old"]
    assert prompts["new"] == prompts["old"] and "SameChild" in prompts["new"]
    assert raw_inner.replace(ARGV_PROMPT, "").endswith(f"-lc '{PRE_CHROME_INNER}'")


@pytest.mark.parametrize("warm_model", ["opus", "sonnet"])
def test_default_spawn_still_claims_a_ready_warm_session(tmp_path, warm_model):
    env, workdir = _launch_env(tmp_path)
    env["AGENTSTACK_HOOKS_DIR"] = str(_hooks_with_warm_pool(tmp_path))
    result = _spawn(tmp_path, env, workdir, "WarmChild", "--model", warm_model)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "warm.log").read_text() == "claim WarmChild\n"


@pytest.mark.parametrize("status", ["opus ready\nsonnet ready", "opus ready (claude-opus-5)\nsonnet ready (claude-sonnet-5)"])
def test_a_warm_session_of_another_or_unknown_model_is_not_claimed(tmp_path, status):
    """A pool started before the aliases moved on would run the old model."""
    env, workdir = _launch_env(tmp_path)
    env["AGENTSTACK_HOOKS_DIR"] = str(_hooks_with_warm_pool(tmp_path))
    env["FAKE_WARM_STATUS"] = status
    result = _spawn(tmp_path, env, workdir, "ColdChild", "--model", "opus")
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "warm.log").exists()
    assert "Cold start" in result.stderr


def test_chrome_request_skips_a_ready_warm_session_and_adds_chrome(tmp_path):
    env, workdir = _launch_env(tmp_path)
    env["AGENTSTACK_HOOKS_DIR"] = str(_hooks_with_warm_pool(tmp_path))
    result = _spawn(tmp_path, env, workdir, "ChromeChild", "--claude-chrome")
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "warm.log").exists()
    assert _new_session(env)[-1].endswith(f"-lc '{LAUNCHED_CHROME_INNER}'")


def test_env_default_turns_chrome_on_for_claude_children(tmp_path):
    env, workdir = _launch_env(tmp_path)
    env["AGENTSTACK_CLAUDE_CHILD_CHROME"] = "1"
    env["AGENTSTACK_CLAUDE_CHILD_CHROME_DEVICE"] = "win-brave.1"
    result = _spawn(tmp_path, env, workdir, "EnvChild")
    assert result.returncode == 0, result.stderr
    assert _new_session(env)[-1].endswith(f"-lc '{LAUNCHED_CHROME_INNER}'")
    assert "deviceId win-brave.1" in _log_text(env)


def test_cli_device_wins_over_env_device(tmp_path):
    env, workdir = _launch_env(tmp_path)
    env["AGENTSTACK_CLAUDE_CHILD_CHROME_DEVICE"] = "from-env"
    result = _spawn(tmp_path, env, workdir, "CliChild", "--claude-chrome-device", "from-cli")
    assert result.returncode == 0, result.stderr
    log = _log_text(env)
    assert "deviceId from-cli" in log
    assert "from-env" not in log


def _codex_inner(env) -> str:
    command = _new_session(env)[-1]
    prefix, _, rest = command.partition(" -lc '")
    assert rest.endswith("'"), command
    return rest[:-1]


def test_env_default_is_ignored_for_codex_children(tmp_path):
    env, workdir = _launch_env(tmp_path, codex=True)
    env["AGENTSTACK_CLAUDE_CHILD_CHROME"] = "1"
    env["AGENTSTACK_CLAUDE_CHILD_CHROME_DEVICE"] = "win-brave"
    result = _spawn(tmp_path, env, workdir, "CodexChild", codex=True)
    assert result.returncode == 0, result.stderr
    log = _log_text(env)
    assert "--chrome" not in log
    assert "Browser:" not in log
    assert "AGENTSTACK_CLAUDE_LAUNCH_ID" not in log
    assert _records(env, "CodexChild") == []


def test_codex_child_process_does_not_inherit_the_chrome_env(tmp_path):
    """Run the Codex launch script the way tmux would, with the Chrome defaults
    in its environment (as when the tmux server itself carries them), and read
    the environment the Codex process actually receives."""
    env, workdir = _launch_env(tmp_path, codex=True)
    result = _spawn(tmp_path, env, workdir, "CodexEnv", codex=True)
    assert result.returncode == 0, result.stderr
    session = _new_session(env)
    inner = _codex_inner(env)
    # tmux hands the launch script its -e variables; since #109 that includes
    # the private file holding the initial task.
    tmux_env = dict(
        session[i + 1].split("=", 1)
        for i, arg in enumerate(session[:-1])
        if arg == "-e" and session[i + 1].startswith("AGENTSTACK_CODEX_PROMPT_FILE=")
    )
    assert tmux_env, session

    dump = tmp_path / "codex-env.txt"
    fake_codex = tmp_path / "fake-codex"
    _executable(fake_codex, f"#!/bin/bash\nenv > {dump}\n")
    hooks = tmp_path / "noop-hooks"
    hooks.mkdir()
    _executable(hooks / "cleanup-child-agent.sh", "#!/bin/bash\nexit 0\n")
    run_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "AGENTSTACK_CODEX_BIN": str(fake_codex),
        "AGENTSTACK_CODEX_MODEL": "gpt-6-sol",
        "AGENTSTACK_HOOKS_DIR": str(hooks),
        "AGENTSTACK_CLAUDE_CHILD_CHROME": "1",
        "AGENTSTACK_CLAUDE_CHILD_CHROME_DEVICE": "from-server",
        **tmux_env,
    }
    subprocess.run(["/bin/bash", "-c", inner], env=run_env, cwd=workdir,
                   capture_output=True, text=True, timeout=60, check=False)
    seen = dump.read_text(encoding="utf-8")
    assert "AGENTSTACK_CODEX_MODEL=gpt-6-sol" in seen  # the fake really ran
    assert "AGENTSTACK_CLAUDE_CHILD_CHROME" not in seen


@pytest.mark.parametrize(
    "flags, env_extra, message",
    [
        (["--codex", "--claude-chrome"], {}, "only valid for Claude children"),
        (["--claude-chrome-device", "bad id;rm"], {}, "must match"),
        (["--claude-chrome-device", "x" * 129], {}, "must match"),
        (["--claude-chrome-device", ""], {}, "requires a deviceId"),
        ([], {"AGENTSTACK_CLAUDE_CHILD_CHROME": "yes"}, "must be 1, 0 or unset"),
        ([], {"AGENTSTACK_CLAUDE_CHILD_CHROME_DEVICE": "a b"}, "must match"),
    ],
)
def test_invalid_requests_stop_before_any_launch(tmp_path, flags, env_extra, message):
    env, workdir = _launch_env(tmp_path)
    env.update(env_extra)
    result = subprocess.run(
        ["/bin/bash", str(SPAWN), "--pre-registered", "BadChild",
         "--child-token-file", str(_handoff(tmp_path)), *flags,
         "task", str(workdir)],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode != 0
    assert message in result.stderr
    assert all(c[0] != "new-session" for c in _calls(env))


# --------------------------------------------------------------------------- #
# first prompt and launch record
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["mail", "embed", "standalone"])
def test_policy_reaches_every_first_prompt(tmp_path, mode):
    env, workdir = _launch_env(tmp_path)
    flags = ["--claude-chrome-device", "win-brave"]
    task = "mail task"
    if mode == "embed":
        task_file = tmp_path / "task.md"
        task_file.write_text("embedded task", encoding="utf-8")
        flags += ["--embed-task", "--task-file", str(task_file)]
    elif mode == "standalone":
        flags.append("--standalone")
        env.pop("PARENT_AGENT")
    result = _spawn(tmp_path, env, workdir, f"Prompt{mode.title()}", *flags, task=task)
    assert result.returncode == 0, result.stderr
    log = _log_text(env)
    assert "The browser to use is deviceId win-brave" in log
    assert "use no browser tool except list_connected_browsers" in log
    assert "Never fall back to another browser" in log
    assert "not a technical lock" in log
    report_to = "the operator" if mode == "standalone" else "your parent agent"
    assert f"report it to {report_to}" in log


def test_without_a_device_the_child_holds_all_browser_actions(tmp_path):
    env, workdir = _launch_env(tmp_path)
    result = _spawn(tmp_path, env, workdir, "NoDevice", "--claude-chrome")
    assert result.returncode == 0, result.stderr
    log = _log_text(env)
    assert "no target browser was specified" in log
    assert "do not select or switch browsers, open tabs, navigate, read pages or click" in log


def test_default_prompt_has_no_browser_policy(tmp_path):
    env, workdir = _launch_env(tmp_path)
    result = _spawn(tmp_path, env, workdir, "Plain")
    assert result.returncode == 0, result.stderr
    assert "Browser:" not in _log_text(env)


def test_chrome_spawn_writes_only_a_pending_record_for_its_launch(tmp_path):
    env, workdir = _launch_env(tmp_path)
    result = _spawn(tmp_path, env, workdir, "Recorded", "--claude-chrome-device", "win-brave")
    assert result.returncode == 0, result.stderr
    launch_id = _launch_id(env)
    record = _state_dir(env) / f"Recorded.claude-launch.pending-{launch_id}.json"
    assert _records(env, "Recorded") == [record]
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert json.loads(record.read_text()) == {
        "version": 2, "agent_name": "Recorded", "launch_id": launch_id,
        "claude_chrome": True, "chrome_device": "win-brave", "standalone": False,
    }


def test_default_spawn_passes_no_launch_id(tmp_path):
    env, workdir = _launch_env(tmp_path)
    assert _spawn(tmp_path, env, workdir, "Plain").returncode == 0
    assert not any(a.startswith("AGENTSTACK_CLAUDE_LAUNCH_ID=") for a in _new_session(env))
    assert _records(env, "Plain") == []


SID = "0123abcd-4567-89ef-0123-456789abcdef"
OTHER_SID = "fedcba98-7654-3210-fedc-ba9876543210"


def _bind(env, name, session_id=SID):
    """What the child's first SessionStart does with its launch id."""
    out = _policy("session", str(_state_dir(env)), name, session_id, _launch_id(env))
    assert out.returncode == 0
    return out.stdout


@pytest.mark.parametrize("second", ["failed-chrome", "inherit", "chrome"])
def test_a_later_launch_never_changes_an_earlier_conversation(tmp_path, second):
    env, workdir = _launch_env(tmp_path)
    assert _spawn(tmp_path, env, workdir, "Reused", "--claude-chrome-device", "win-original").returncode == 0
    assert "deviceId win-original" in _bind(env, "Reused")

    pathlib.Path(env["FAKE_TMUX_ALIVE"]).unlink()
    pathlib.Path(env["FAKE_TMUX_LOG"]).unlink()
    flags = [] if second == "inherit" else ["--claude-chrome-device", "mac-after"]
    if second == "failed-chrome":
        tmux = pathlib.Path(env["PATH"].split(":")[0]) / "tmux"
        tmux.write_text(tmux.read_text().replace(
            "  new-session) :", "  new-session) exit 42 ;;\n  unused) :"), encoding="utf-8")
    result = _spawn(tmp_path, env, workdir, "Reused", *flags)
    assert (result.returncode != 0) == (second == "failed-chrome"), result.stderr

    policy = _load_policy()
    state = policy.session_record(str(_state_dir(env)), "Reused", SID)
    assert state["chrome_device"] == "win-original"
    if second == "failed-chrome":
        # The failed launch removed its own pending record, nothing else.
        assert _records(env, "Reused") == [_state_dir(env) / f"Reused.claude-launch.{SID}.json"]
    if second == "chrome":
        assert "deviceId mac-after" in _bind(env, "Reused", OTHER_SID)
        assert policy.session_record(str(_state_dir(env)), "Reused", SID)["chrome_device"] == "win-original"


def test_chrome_spawn_stops_when_the_record_cannot_be_written(tmp_path):
    env, workdir = _launch_env(tmp_path)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for entry in (ROOT / "hooks").iterdir():
        if entry.name != "claude_chrome_policy.py":
            (hooks / entry.name).symlink_to(entry)
    _executable(hooks / "claude_chrome_policy.py",
                "import sys\nif sys.argv[1] == 'prepare': sys.exit(1)\n"
                f"exec(open({str(POLICY)!r}).read())\n")
    env["AGENTSTACK_HOOKS_DIR"] = str(hooks)
    result = _spawn(tmp_path, env, workdir, "Blocked", "--claude-chrome")
    assert result.returncode != 0
    assert "could not write the Claude in Chrome launch record" in result.stderr
    assert all(c[0] != "new-session" for c in _calls(env))


# --------------------------------------------------------------------------- #
# policy helper and session-start reminder
# --------------------------------------------------------------------------- #
def _policy(*args):
    return subprocess.run(
        [sys.executable, str(POLICY), *args], text=True, capture_output=True, check=False
    )


def _load_policy():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ccp_under_test", POLICY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LAUNCH = "11111111-2222-3333-4444-555555555555"


def test_session_binding_resume_clear_and_bad_records(tmp_path):
    d = str(tmp_path)
    # No record and no launch id: an ordinary session, nothing printed.
    assert _policy("session", d, "A", SID, "").stdout == ""
    assert _policy("prepare", d, "A", LAUNCH, "dev-1", "0").returncode == 0
    # First start binds the pending record to the session and consumes it.
    assert "deviceId dev-1" in _policy("session", d, "A", SID, LAUNCH).stdout
    assert not (tmp_path / f"A.claude-launch.pending-{LAUNCH}.json").exists()
    bound = tmp_path / f"A.claude-launch.{SID}.json"
    assert json.loads(bound.read_text())["session_id"] == SID
    # Resume / compaction: same session id, launch id may be gone.
    assert "deviceId dev-1" in _policy("session", d, "A", SID, "").stdout
    # /clear in the same process: new session id, same launch id.
    assert "deviceId dev-1" in _policy("session", d, "A", OTHER_SID, LAUNCH).stdout
    # A launch id with no record, or another agent's record: no browser.
    out = _policy("session", d, "A", "99999999-0000-0000-0000-000000000000",
                  "22222222-2222-2222-2222-222222222222").stdout
    assert "Use no browser tool" in out
    assert "Use no browser tool" in _policy("session", d, "B", SID, LAUNCH).stdout
    # Tampered records.
    bound.write_text("{not json", encoding="utf-8")
    assert "Use no browser tool" in _policy("session", d, "A", SID, "").stdout
    other = tmp_path / f"A.claude-launch.{OTHER_SID}.json"
    other.chmod(0o644)
    assert "0600" in _policy("session", d, "A", OTHER_SID, "").stdout


def test_a_failed_binding_write_still_says_no_browser(tmp_path):
    d = tmp_path / "state"
    assert _policy("prepare", str(d), "A", LAUNCH, "dev-1", "0").returncode == 0
    d.chmod(0o500)
    try:
        out = _policy("session", str(d), "A", SID, LAUNCH)
    finally:
        d.chmod(0o700)
    assert out.returncode == 0
    assert "could not be saved" in out.stdout
    assert "Use no browser tool" in out.stdout


def test_a_record_renamed_to_another_session_is_rejected(tmp_path):
    d = str(tmp_path)
    assert _policy("prepare", d, "A", LAUNCH, "dev-1", "0").returncode == 0
    _policy("session", d, "A", SID, LAUNCH)
    (tmp_path / f"A.claude-launch.{SID}.json").rename(tmp_path / f"A.claude-launch.{OTHER_SID}.json")
    out = _policy("session", d, "A", OTHER_SID, "").stdout
    assert "different session" in out


def test_reminder_binds_and_repeats_the_policy_at_session_start(tmp_path):
    runtime = tmp_path / "runtime"
    state = runtime / "child-agents"
    assert _policy("prepare", str(state), "HookChild", LAUNCH, "win-brave", "0").returncode == 0
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "AGENT_NAME": "HookChild",
        "AGENTSTACK_RESERVED_IDENTITY": "1",
        "AGENTSTACK_RUNTIME_DIR": str(runtime),
        "AGENTSTACK_HOOKS_DIR": str(ROOT / "hooks"),
        "AGENTSTACK_MCP_URL": "http://127.0.0.1:9/mcp",
        "PROJECT_KEY": "/shared/project",
        "AGENTSTACK_PROJECT_KEY": "/shared/project",
        "AGENTSTACK_CLAUDE_LAUNCH_ID": LAUNCH,
    }
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    result = subprocess.run(
        ["/bin/bash", str(REMINDER)], input=json.dumps({"session_id": SID}),
        env=env, text=True, capture_output=True, timeout=60, check=False, cwd=tmp_path,
    )
    assert "The browser to use is deviceId win-brave" in result.stdout, result.stdout
    assert (state / f"HookChild.claude-launch.{SID}.json").exists()


# --------------------------------------------------------------------------- #
# dashboard: resume and NEW AGENT
# --------------------------------------------------------------------------- #
def _bound_record(tmp_path, name="ResumeChild", session_id=SID, device="win-brave"):
    d = str(tmp_path / "runtime" / "child-agents")
    assert _policy("prepare", d, name, LAUNCH, device, "0").returncode == 0
    _policy("session", d, name, session_id, LAUNCH)
    return pathlib.Path(d) / f"{name}.claude-launch.{session_id}.json"


def _resume(monkeypatch, tmp_path, session="ResumeChild", transcript_text=""):
    transcript = tmp_path / f"{SID}.jsonl"
    transcript.write_text(transcript_text, encoding="utf-8")
    claude = tmp_path / "claude"
    claude.write_text("", encoding="utf-8")
    launched = []
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    token = runtime / f"agent_token_{session}"
    token.write_text("fixture-owner-token", encoding="utf-8")
    token.chmod(0o600)
    registration = {"agent_id": 73, "agent_name": session, "project_key": "/fixture/project", "program": "claude-code"}
    monkeypatch.setattr(server, "_claude_registration", lambda _n: registration)
    monkeypatch.setattr(server, "_mcp_call", lambda method, args: {
        "ok": True, "data": {"id": 73, "name": session} if method == "register_agent"
        else {"status": "active", "agent_name": session, "project_key": registration["project_key"]},
    })
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(server, "HOOKS_DIR", str(ROOT / "hooks"))
    monkeypatch.setattr(server, "ABS_CLAUDE", str(claude))
    monkeypatch.setattr(server, "_agent_program", lambda _n: "claude-code")
    monkeypatch.setattr(server, "_transcript_path", lambda _n: str(transcript))
    monkeypatch.setattr(server, "_transcript_cwd", lambda _p: str(tmp_path))
    monkeypatch.setattr(
        server, "_launch_claude_resume_tmux",
        lambda argv, **_k: launched.append(argv) or {"ok": False, "error": "test"},
    )
    result = server.do_resume(session)
    return result, launched


def test_resume_without_a_record_is_unchanged(monkeypatch, tmp_path):
    _result, launched = _resume(monkeypatch, tmp_path)
    assert launched[0][-1].endswith(f"--resume {SID} -n ResumeChild")


def test_resume_of_a_chrome_child_adds_chrome(monkeypatch, tmp_path):
    _bound_record(tmp_path)
    _result, launched = _resume(monkeypatch, tmp_path)
    inner = launched[0][-1]
    assert inner.endswith(f"--resume {SID} -n ResumeChild --chrome")
    assert f"export AGENTSTACK_CLAUDE_LAUNCH_ID={LAUNCH}; " in inner
    assert f"export AGENTSTACK_RUNTIME_DIR={tmp_path / 'runtime'}; " in inner


def test_clear_after_resume_stays_bound_to_the_launch(monkeypatch, tmp_path):
    """resume -> /clear (new session id) -> resume of the cleared session."""
    _bound_record(tmp_path)
    _result, launched = _resume(monkeypatch, tmp_path)
    launch_id = launched[0][-1].split("AGENTSTACK_CLAUDE_LAUNCH_ID=", 1)[1].split(";", 1)[0]
    d = str(tmp_path / "runtime" / "child-agents")
    # The resumed process's SessionStart after /clear:
    out = _policy("session", d, "ResumeChild", OTHER_SID, launch_id).stdout
    assert "deviceId win-brave" in out
    state = _load_policy().session_record(d, "ResumeChild", OTHER_SID)
    assert state["launch_id"] == LAUNCH


def test_transcript_text_never_decides_the_resume(monkeypatch, tmp_path):
    """Quoted launch ids or policy text in a transcript (tool output, another
    agent's log) must not turn an inherit conversation into a Chrome one."""
    d = str(tmp_path / "runtime" / "child-agents")
    assert _policy("prepare", d, "ResumeChild", LAUNCH, "win-brave", "0").returncode == 0
    quoted = json.dumps({"type": "user", "message": {"content": [{
        "type": "tool_result",
        "content": f"Browser: ... deviceId win-brave ... AGENTSTACK_CLAUDE_LAUNCH_ID={LAUNCH} "
                   f"(Claude in Chrome launch id: {LAUNCH})"}]}}) + "\n"
    result, launched = _resume(monkeypatch, tmp_path, transcript_text=quoted)
    assert launched[0][-1].endswith(f"--resume {SID} -n ResumeChild")


def test_known_limitation_unsaved_clear_session_resumes_as_inherit(monkeypatch, tmp_path):
    """Documented limitation, not a fix: when the record for a new session id
    (after /clear) cannot be saved, the hook says "no browser" at that moment,
    but a later resume of that session id has no record and resumes as inherit
    -- the chosen browser is not restored and no browser ban is enforced."""
    d = tmp_path / "runtime" / "child-agents"
    _bound_record(tmp_path, session_id=OTHER_SID)  # the original session
    d.chmod(0o500)
    try:
        out = _policy("session", str(d), "ResumeChild", SID, LAUNCH).stdout
    finally:
        d.chmod(0o700)
    assert "Use no browser tool" in out
    assert not (d / f"ResumeChild.claude-launch.{SID}.json").exists()
    _result, launched = _resume(monkeypatch, tmp_path)
    assert launched[0][-1].endswith(f"--resume {SID} -n ResumeChild")


def test_resume_ignores_records_of_other_conversations(monkeypatch, tmp_path):
    # A later launch under the same name (another session id) and a launch
    # that never started (pending only) do not affect this conversation.
    _bound_record(tmp_path, session_id=OTHER_SID)
    d = str(tmp_path / "runtime" / "child-agents")
    assert _policy("prepare", d, "ResumeChild",
                   "33333333-3333-3333-3333-333333333333", "mac", "0").returncode == 0
    _result, launched = _resume(monkeypatch, tmp_path)
    assert launched[0][-1].endswith(f"--resume {SID} -n ResumeChild")


@pytest.mark.parametrize("damage", ["json", "agent", "mode", "session"])
def test_resume_stops_on_an_invalid_record(monkeypatch, tmp_path, damage):
    record = _bound_record(tmp_path)
    if damage == "json":
        record.write_text("{", encoding="utf-8")
    elif damage == "agent":
        state = json.loads(record.read_text())
        record.write_text(json.dumps({**state, "agent_name": "Other"}))
    elif damage == "session":
        state = json.loads(record.read_text())
        record.write_text(json.dumps({**state, "session_id": OTHER_SID}))
    else:
        record.chmod(0o644)
    result, launched = _resume(monkeypatch, tmp_path)
    assert launched == []
    assert result["ok"] is False
    assert "Claude in Chrome launch record is invalid" in result["error"]


def _spawn_spec(monkeypatch, payload):
    captured = []
    monkeypatch.setattr(server, "_spawn_unavailable_error", lambda: None)
    monkeypatch.setattr(server, "_spawn_request", lambda _p: ({}, None))
    monkeypatch.setattr(server, "_claude_catalog", lambda: type("C", (), {"error": None, "source": "local_cache", "models": ()})())
    monkeypatch.setattr(
        server, "spawn_with_launch_spec",
        lambda _payload, spec: captured.append(spec) or {"ok": True},
    )
    result = server.do_spawn(payload)
    return result, captured


@pytest.mark.parametrize(
    "extra, args",
    [
        ({}, ()),
        ({"claude_chrome": False}, ()),
        ({"claude_chrome": True}, ("--claude-chrome",)),
        ({"claude_chrome": True, "claude_chrome_device": " win-brave "},
         ("--claude-chrome-device", "win-brave")),
        ({"claude_chrome_device": "win-brave"}, ("--claude-chrome-device", "win-brave")),
    ],
)
def test_new_agent_passes_the_explicit_request(monkeypatch, extra, args):
    result, captured = _spawn_spec(
        monkeypatch, {"task": "t", "model": "claude-opus-5-5", **extra})
    assert result == {"ok": True}
    assert captured[0].provider_args == args


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"claude_chrome": "true"}, "must be a boolean"),
        ({"claude_chrome_device": 3}, "must be a string"),
        ({"claude_chrome": False, "claude_chrome_device": "d"}, "requires claude_chrome"),
        ({"claude_chrome_device": "a b"}, "must match"),
        ({"provider": "codex", "claude_chrome": True}, "not supported for provider"),
        ({"provider": "codex", "claude_chrome_device": "d"}, "not supported for provider"),
    ],
)
def test_new_agent_rejects_invalid_requests(monkeypatch, payload, message):
    result, captured = _spawn_spec(monkeypatch, {"task": "t", **payload})
    assert result["ok"] is False
    assert message in result["error"]
    assert captured == []


def test_new_agent_strips_the_cli_env_defaults_from_the_launcher(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTSTACK_CLAUDE_CHILD_CHROME", "1")
    monkeypatch.setenv("AGENTSTACK_CLAUDE_CHILD_CHROME_DEVICE", "from-env")
    launcher = tmp_path / "launcher.sh"
    launcher.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    seen = []

    def mcp(method, args, timeout=15):
        data = {"id": 73, "name": "QuietCurie", "registration_token": "tok"} \
            if method == "register_agent" else {}
        return {"ok": True, "data": data}

    def popen(args, **kwargs):
        seen.append((args, kwargs.get("env") or {}))
        raise OSError("stop after capture")

    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(server, "_project_key", lambda: "/project")
    monkeypatch.setattr(server, "_spawn_name_status", lambda _: "available")
    monkeypatch.setattr(server, "_mcp_call", mcp)
    monkeypatch.setattr(server, "_spawn_unavailable_error", lambda: None)
    monkeypatch.setattr(server.subprocess, "Popen", popen)
    spec = server.SpawnLaunchSpec(
        provider="claude", program="claude-code", model="claude-opus-5-5",
        script=str(launcher),
    )
    server.spawn_with_launch_spec(
        {"standalone": True, "name": "QuietCurie", "task": "t", "dir": str(tmp_path)},
        spec,
    )
    assert seen, "launcher was not started"
    args, env = seen[0]
    assert "--claude-chrome" not in args and "--claude-chrome-device" not in args
    assert "AGENTSTACK_CLAUDE_CHILD_CHROME" not in env
    assert "AGENTSTACK_CLAUDE_CHILD_CHROME_DEVICE" not in env


def test_new_agent_form_offers_chrome_only_for_claude():
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert 'id="spm-chrome"' in html
    assert "Explicitly enable Claude in Chrome (--chrome)" in html
    assert "Unchecked follows your Claude settings." in html
    assert "payload.provider==='claude'&&SPM('spm-chrome')" in html
    assert "provider.id==='claude'" in html
