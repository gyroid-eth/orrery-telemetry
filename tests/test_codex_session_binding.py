"""End-to-end fixtures for Codex launch expectations and SessionStart receipts."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_teardown import TEST_LABEL_PREFIX  # noqa: E402

from dashboard import server


ROOT = Path(__file__).resolve().parents[1]
CODEX_APP_INSTALLER = ROOT / "scripts" / "install-codex-app-integration.sh"
CODEX_MARKETPLACE_BUILDER = ROOT / "scripts" / "build-codex-app-marketplace.py"
HAVE_CODEX_CLI = shutil.which("codex") is not None
needs_codex_cli = pytest.mark.skipif(
    not HAVE_CODEX_CLI,
    reason="requires the Codex CLI to exercise the selected plugin cache",
)
AGENT = "BoundCodex"
AGENT_ID = 73
SESSION_ID = "01d13f58-8e1a-7777-a3d8-e2ba243cdb49"


def _module(relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare_mod = _module("hooks/prepare-codex-session-binding.py", "prepare_codex_binding")
record_mod = _module(
    "integrations/codex_app/plugin/scripts/record-codex-session-index.py",
    "record_codex_binding",
)


def _registration(project: Path) -> dict:
    return {
        "agent_id": AGENT_ID,
        "agent_name": AGENT,
        "project_key": str(project),
        "program": "codex",
    }


def _rollout(path: Path, session_id: str = SESSION_ID) -> None:
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": str(path.parent)},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _payload(transcript: Path, **overrides: object) -> dict:
    payload = {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "session_id": SESSION_ID,
        "transcript_path": str(transcript),
    }
    payload.update(overrides)
    return payload


def _old_cached_plugin(tmp_path: Path) -> dict[str, object]:
    """Use the real CLI to select the pre-recorder form of the current plugin."""

    home = tmp_path / "plugin-home"
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    install_dir = home / ".agentstack" / "integrations" / "codex_app"
    shutil.copytree(ROOT / "integrations" / "codex_app", install_dir)
    plugin = install_dir / "plugin"
    runner = plugin / "scripts" / "run-hook.sh"
    text = runner.read_text(encoding="utf-8")
    start = text.index("# CLI history binding is deliberately separate")
    end = text.index("printf '%s' \"$payload\" | exec", start)
    runner.write_text(text[:start] + text[end:], encoding="utf-8")
    (plugin / "scripts" / "record-codex-session-index.py").unlink()

    marketplace = install_dir / "marketplace"
    subprocess.run(
        [
            sys.executable,
            str(CODEX_MARKETPLACE_BUILDER),
            str(install_dir),
            str(marketplace),
            "--marketplace-name",
            "agentstack-local",
        ],
        check=True,
    )
    environment = os.environ.copy()
    environment.update({"HOME": str(home), "CODEX_HOME": str(codex_home)})
    environment.pop("AGENTSTACK_CODEX_APP_INSTALL_DIR", None)
    environment.pop("AGENTSTACK_CODEX_APP_RUNTIME_DIR", None)
    subprocess.run(
        ["codex", "plugin", "marketplace", "add", str(marketplace), "--json"],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    installed = subprocess.run(
        [
            "codex",
            "plugin",
            "add",
            "agentstack-codex-app@agentstack-local",
            "--json",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    cached = Path(json.loads(installed.stdout)["installedPath"])
    assert not (cached / "scripts" / "record-codex-session-index.py").exists()

    # Reproduce the core install boundary: approved plugin/src are deployed,
    # while the already-selected marketplace/cache remain on the old runner.
    for name in ("plugin", "src"):
        shutil.copytree(
            ROOT / "integrations" / "codex_app" / name,
            install_dir / name,
            dirs_exist_ok=True,
        )
    return {
        "home": home,
        "environment": environment,
        "install_dir": install_dir,
        "cached": cached,
    }


@pytest.fixture()
def binding_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    runtime = tmp_path / "runtime"
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "sessions" / "rollout.jsonl"
    transcript.parent.mkdir()
    _rollout(transcript)

    db = tmp_path / "mail.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT);
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                name TEXT,
                program TEXT,
                last_active_ts TEXT
            );
            """
        )
        connection.execute("INSERT INTO projects VALUES (1, ?)", (str(project),))
        connection.execute(
            "INSERT INTO agents VALUES (?, 1, ?, 'codex', '2026-09-15 08:00:00')",
            (AGENT_ID, AGENT),
        )

    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "PROJECT_KEY", str(project))
    monkeypatch.setattr(server, "VAULT", "")
    monkeypatch.setattr(server, "SESSION_INDEX_DIR", str(runtime / "session_index"))
    monkeypatch.setattr(server, "CODEX_LAUNCH_DIR", str(runtime / "codex_launches"))
    return {
        "runtime": runtime,
        "project": project,
        "transcript": transcript,
        "registration": _registration(project),
    }


def _prepare(env: dict, *, now: float = 100.0, history_mode: str = "enabled"):
    return prepare_mod.prepare(
        env["runtime"],
        env["registration"],
        launch_kind="startup",
        history_mode=history_mode,
        now=now,
    )


def _record(env: dict, launch_path: Path, launch_id: str, **overrides: object) -> str:
    return record_mod.record_payload(
        _payload(env["transcript"], **overrides),
        launch_path=launch_path,
        launch_id=launch_id,
    )


def _adopt_child_handoff(
    tmp_path: Path,
    *,
    runtime: Path,
    agent_name: str,
    project_key: str,
    token: Path,
) -> Path:
    """Run spawn_child.sh's real handoff contract without starting tmux."""

    spawn = (ROOT / "hooks" / "spawn_child.sh").read_text(encoding="utf-8")
    start = spawn.index("child_token_file_path() {")
    end = spawn.index("\n# Restore the canonical token file", start)
    sidecar = token.with_name(token.name + ".binding.json")
    script = (
        f'RUNTIME_DIR="{runtime}"\n'
        'CHILD_STATE_DIR="$RUNTIME_DIR/child-agents"\n'
        + spawn[start:end]
        + f'\nadopt_child_token_file "{agent_name}" "{project_key}" '
        f'"{token}" true "{sidecar}"\n'
    )
    adopted = subprocess.run(
        ["bash", "-c", script], text=True, capture_output=True, check=False
    )
    assert adopted.returncode == 0, adopted.stderr
    state_path = runtime / "child-agents" / f"{agent_name}.json"
    assert state_path.is_file(), {
        "stdout": adopted.stdout,
        "stderr": adopted.stderr,
        "token": str(token),
        "sidecar": str(sidecar),
    }
    return state_path


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _codex_entrypoint_layout(tmp_path: Path, layout: str) -> dict[str, Path]:
    if layout == "source":
        root = ROOT
    else:
        root = tmp_path / "installed-agentstack"
        for relative in (
            "bin/agentstack-preregister-child",
            "bin/lib/agentstack-register.sh",
            "bin/lib/agentstack-scientists.sh",
            "hooks/spawn_child.sh",
            "hooks/prepare-codex-session-binding.py",
            "integrations/codex_app/plugin/scripts/record-codex-session-index.py",
        ):
            source = ROOT / relative
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return {
        "root": root,
        "preregister": root / "bin" / "agentstack-preregister-child",
        "register_lib": root / "bin" / "lib" / "agentstack-register.sh",
        "spawn": root / "hooks" / "spawn_child.sh",
        "hooks": root / "hooks",
        "recorder": (
            root
            / "integrations"
            / "codex_app"
            / "plugin"
            / "scripts"
            / "record-codex-session-index.py"
        ),
    }


def _fake_codex_launch_env(
    tmp_path: Path, *, runtime: Path, project: Path, layout: dict[str, Path]
) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    tmux_log = tmp_path / "tmux.log"
    tmux_alive = tmp_path / "tmux.alive"
    _executable(
        fake_bin / "tmux",
        "#!/bin/bash\n"
        "{ printf 'CALL'; for arg in \"$@\"; do printf '\\034%s' \"$arg\"; done; "
        "printf '\\035\\n'; } >> \"$FAKE_TMUX_LOG\"\n"
        "case \"${1:-}\" in\n"
        "  new-session) : > \"$FAKE_TMUX_ALIVE\" ;;\n"
        "  capture-pane) printf '\\ngpt-5.6-terra medium · ~/workspace\\n' ;;\n"
        "  has-session) [[ -f \"$FAKE_TMUX_ALIVE\" ]] ;;\n"
        "  kill-session) rm -f \"$FAKE_TMUX_ALIVE\" ;;\n"
        "  display-message) printf 'ParentAgent\\n' ;;\n"
        "esac\n",
    )
    _executable(fake_bin / "sleep", "#!/bin/bash\nexit 0\n")
    _executable(
        fake_bin / "codex",
        "#!/bin/bash\n"
        "if [[ \"${1:-}\" == --help ]]; then\n"
        "  printf '%s\\n' '  --ask-for-approval <POLICY>'\n"
        "fi\n",
    )
    _executable(fake_bin / "claude", "#!/bin/bash\nexit 0\n")

    fake_register = tmp_path / "fake-register.sh"
    fake_register.write_text(
        f'. "{layout["register_lib"]}"\n'
        "ags_mail_load_token() { :; }\n"
        "ags_has_scientist_suffix() { return 0; }\n"
        "ags_generate_registration_token() { printf 'sent-token\\n'; }\n"
        "ags_mcp_call() {\n"
        "  if [[ \"$1\" == register_agent ]]; then\n"
        "    printf '{\"id\":73,\"name\":\"BoundCodex\","
        "\"registration_token\":\"server-owner-token\"}\\n'\n"
        "  else\n"
        "    printf '{\"id\":1}\\n'\n"
        "  fi\n"
        "}\n"
        "ags_mcp_has_error() { return 1; }\n"
        "ags_extract_agent_name() { "
        "python3 -c 'import json,sys; print(json.load(sys.stdin)[\"name\"])'; }\n"
        "ags_extract_agent_id() { "
        "python3 -c 'import json,sys; print(json.load(sys.stdin)[\"id\"])'; }\n"
        "ags_extract_registration_token() { "
        "python3 -c 'import json,sys; print(json.load(sys.stdin)[\"registration_token\"])'; }\n"
        "ags_apply_contact_policy() { :; }\n"
        "ags_record_name_substitution() { :; }\n",
        encoding="utf-8",
    )

    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    workdir = tmp_path / "workdir"
    home.mkdir()
    codex_home.mkdir()
    workdir.mkdir()
    env = os.environ.copy()
    for inherited in (
        "AGENT_NAME",
        "CHILD_REGISTRATION_TOKEN",
        "AGENTSTACK_CODEX_LAUNCH_BINDING",
        "AGENTSTACK_CODEX_LAUNCH_ID",
    ):
        env.pop(inherited, None)
    env.update(
        {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "HOME": str(home),
            "CODEX_HOME": str(codex_home),
            "PARENT_AGENT": "ParentAgent",
            "PROJECT_KEY": str(project),
            "AGENTSTACK_PROJECT_KEY": str(project),
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_HOOKS_DIR": str(layout["hooks"]),
            "AGENTSTACK_HOME": str(layout["root"]),
            "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
            "AGENTSTACK_REGISTER_LIB": str(fake_register),
            "AGENTSTACK_ENV_FILE": "",
            "AGENTSTACK_MCP_PROXY": str(tmp_path / "missing-proxy"),
            "AGENTSTACK_MCP_URL": "http://127.0.0.1:9/mcp",
            "AGENTSTACK_MAIL_ENV": str(tmp_path / "missing-mail-env"),
            "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled",
            "AGENTSTACK_MANAGED_AGENTS_FILE": str(runtime / "managed_agents.txt"),
            "AGENTSTACK_TERMINAL": "none",
            "AGENTSTACK_PYTHON": sys.executable,
            "FAKE_TMUX_LOG": str(tmux_log),
            "FAKE_TMUX_ALIVE": str(tmux_alive),
        }
    )
    return env, workdir


def _tmux_new_session_env(log_path: Path) -> tuple[dict[str, str], list[str]]:
    calls = []
    for record in log_path.read_text(encoding="utf-8").split("\x1d\n"):
        if not record:
            continue
        assert record.startswith("CALL\x1c")
        calls.append(record.removeprefix("CALL\x1c").split("\x1c"))
    command = next(call for call in calls if call[0] == "new-session")
    child_env: dict[str, str] = {}
    for index, argument in enumerate(command[:-1]):
        if argument == "-e":
            key, value = command[index + 1].split("=", 1)
            child_env[key] = value
    return child_env, command


def _run_preregistered_codex_spawn(
    *,
    layout: dict[str, Path],
    env: dict[str, str],
    workdir: Path,
    task_file: Path,
    handoff: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    args = ["/bin/bash", str(layout["spawn"]), "--pre-registered", AGENT]
    if handoff is not None:
        args.extend(["--child-token-file", str(handoff)])
    args.extend(
        [
            "--codex",
            "--model",
            "gpt-5.6-terra",
            "--effort",
            "medium",
            "--embed-task",
            "--task-file",
            str(task_file),
            str(workdir),
        ]
    )
    return subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize(
    ("layout_name", "restore_token_from_state"),
    [("source", False), ("installed", False), ("source", True)],
)
def test_preregister_no_arg_spawn_reaches_recorder_and_reader(
    binding_env, tmp_path: Path, layout_name: str, restore_token_from_state: bool
) -> None:
    layout = _codex_entrypoint_layout(tmp_path, layout_name)
    env, workdir = _fake_codex_launch_env(
        tmp_path,
        runtime=binding_env["runtime"],
        project=binding_env["project"],
        layout=layout,
    )
    task_file = tmp_path / "task.md"
    task_file.write_text("Verify the canonical Codex registration binding.", encoding="utf-8")
    handoff = tmp_path / "token-BoundCodex"
    state_path = binding_env["runtime"] / "child-agents" / f"{AGENT}.json"
    assert not state_path.exists()

    preregister = subprocess.run(
        [
            str(layout["preregister"]),
            "--project-key",
            str(binding_env["project"]),
            "--name",
            AGENT,
            "--program",
            "codex",
            "--model",
            "gpt-5.6-terra",
            "--task-description",
            "fixture child",
            "--token-file-out",
            str(handoff),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert preregister.returncode == 0, preregister.stderr
    assert preregister.stdout.strip() == AGENT
    sidecar = handoff.with_name(handoff.name + ".binding.json")
    assert handoff.is_file() and sidecar.is_file()
    canonical_token = binding_env["runtime"] / f"agent_token_{AGENT}"
    assert canonical_token.is_file() and state_path.is_file()
    assert canonical_token.stat().st_mode & 0o777 == 0o600
    assert state_path.stat().st_mode & 0o777 == 0o600

    # The documented no-arg launch may happen after the caller has discarded
    # its temporary handoff. Nothing in the fixture copies the sidecar into
    # child state; preregister itself must have persisted the formal receipt.
    handoff.unlink()
    sidecar.unlink()
    if restore_token_from_state:
        canonical_token.unlink()
    spawn = _run_preregistered_codex_spawn(
        layout=layout,
        env=env,
        workdir=workdir,
        task_file=task_file,
    )
    assert spawn.returncode == 0, spawn.stderr
    assert spawn.stdout.strip() == AGENT

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert canonical_token.is_file()
    assert state == {
        "agent_id": AGENT_ID,
        "agent_name": AGENT,
        "program": "codex",
        "project_key": str(binding_env["project"]),
        "registration_token": "server-owner-token",
    }
    assert handoff.exists() is False and sidecar.exists() is False

    child_env, command = _tmux_new_session_env(Path(env["FAKE_TMUX_LOG"]))
    launch_path = Path(child_env["AGENTSTACK_CODEX_LAUNCH_BINDING"])
    launch_id = child_env["AGENTSTACK_CODEX_LAUNCH_ID"]
    assert child_env["AGENTSTACK_CODEX_MODEL"] == "gpt-5.6-terra"
    assert child_env["AGENTSTACK_CODEX_EFFORT"] == "medium"
    assert launch_path == binding_env["runtime"] / "codex_launches" / f"{AGENT_ID}.json"
    launch = json.loads(launch_path.read_text(encoding="utf-8"))
    assert launch["launch_id"] == launch_id
    assert launch["agent_id"] == AGENT_ID
    assert launch["project_key"] == str(binding_env["project"])
    assert "$AGENTSTACK_CODEX_BIN" in command[-1]
    combined_output = preregister.stdout + preregister.stderr + spawn.stdout + spawn.stderr
    assert "server-owner-token" not in combined_output
    assert "server-owner-token" not in Path(env["FAKE_TMUX_LOG"]).read_text(
        encoding="utf-8"
    )

    recorder_env = env.copy()
    recorder_env.update(
        {
            "AGENTSTACK_CODEX_LAUNCH_BINDING": str(launch_path),
            "AGENTSTACK_CODEX_LAUNCH_ID": launch_id,
        }
    )
    recorded = subprocess.run(
        [sys.executable, str(layout["recorder"])],
        input=json.dumps(_payload(binding_env["transcript"])),
        cwd=ROOT,
        env=recorder_env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert recorded.returncode == 0, recorded.stderr
    assert server._codex_history_binding(AGENT, now=200.0)["history_binding"] == "bound"


@needs_codex_cli
def test_refreshed_cli_cache_runner_reaches_recorder_and_reader(
    binding_env, tmp_path: Path
) -> None:
    plugin_fixture = _old_cached_plugin(tmp_path)
    cached = plugin_fixture["cached"]
    assert isinstance(cached, Path)
    environment = dict(plugin_fixture["environment"])
    install_dir = plugin_fixture["install_dir"]
    assert isinstance(install_dir, Path)
    launch_path, launch_id = _prepare(binding_env)
    hook_environment = dict(environment)
    hook_environment.update(
        {
            "AGENTSTACK_CODEX_APP_INSTALL_DIR": str(install_dir),
            "AGENTSTACK_CODEX_LAUNCH_BINDING": str(launch_path),
            "AGENTSTACK_CODEX_LAUNCH_ID": launch_id,
            "AGENTSTACK_PYTHON": sys.executable,
        }
    )
    payload = json.dumps(_payload(binding_env["transcript"]))
    cached_runner = cached / "scripts" / "run-hook.sh"

    before_refresh = subprocess.run(
        [str(cached_runner)],
        input=payload,
        env=hook_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert before_refresh.returncode == 0, before_refresh.stderr
    assert not (binding_env["runtime"] / "session_index" / f"{AGENT_ID}.json").exists()
    assert server._codex_history_binding(AGENT, now=200.0)["history_binding"] == (
        "unconfirmed"
    )

    refreshed = subprocess.run(
        [
            str(CODEX_APP_INSTALLER),
            "--refresh-plugin-only",
            "--install-dir",
            str(install_dir),
            "--marketplace-name",
            "agentstack-local",
            "--python-bin",
            sys.executable,
            "--codex-bin",
            shutil.which("codex") or "codex",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert refreshed.returncode == 0, refreshed.stderr
    assert "Plugin refresh complete" in refreshed.stdout
    assert (cached / "scripts" / "record-codex-session-index.py").is_file()
    assert cached_runner.read_bytes() == (
        ROOT / "integrations" / "codex_app" / "plugin" / "scripts" / "run-hook.sh"
    ).read_bytes()

    recorded = subprocess.run(
        [str(cached_runner)],
        input=payload,
        env=hook_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert recorded.returncode == 0, recorded.stderr
    assert server._codex_history_binding(AGENT, now=200.0)["history_binding"] == "bound"


@pytest.mark.parametrize(
    "failure_mode",
    [
        "missing",
        "corrupt",
        "project_mismatch",
        "name_mismatch",
        "provider_mismatch",
        "token_mismatch",
        "prepare_failure",
    ],
)
def test_no_arg_codex_spawn_rejects_untrusted_canonical_state_before_cli(
    binding_env, tmp_path: Path, failure_mode: str
) -> None:
    layout = _codex_entrypoint_layout(tmp_path, "source")
    env, workdir = _fake_codex_launch_env(
        tmp_path,
        runtime=binding_env["runtime"],
        project=binding_env["project"],
        layout=layout,
    )
    task_file = tmp_path / "task.md"
    task_file.write_text("Must not reach Codex.", encoding="utf-8")
    runtime = binding_env["runtime"]
    runtime.mkdir(parents=True, exist_ok=True)
    canonical_token = runtime / f"agent_token_{AGENT}"
    canonical_token.write_text("server-owner-token", encoding="utf-8")
    canonical_token.chmod(0o600)
    state_path = runtime / "child-agents" / f"{AGENT}.json"
    state_path.parent.mkdir(mode=0o700)
    state = {
        "agent_id": AGENT_ID,
        "agent_name": AGENT,
        "program": "codex",
        "project_key": str(binding_env["project"]),
        "registration_token": "server-owner-token",
    }
    if failure_mode == "corrupt":
        state_path.write_text("{", encoding="utf-8")
    elif failure_mode != "missing":
        if failure_mode == "project_mismatch":
            state["project_key"] = str(tmp_path / "other-project")
        elif failure_mode == "name_mismatch":
            state["agent_name"] = "OtherCodex"
        elif failure_mode == "provider_mismatch":
            state["program"] = "claude-code"
        elif failure_mode == "token_mismatch":
            state["registration_token"] = "older-registration-token"
        state_path.write_text(json.dumps(state), encoding="utf-8")
    if state_path.exists():
        state_path.chmod(0o600)
    if failure_mode == "prepare_failure":
        empty_hooks = tmp_path / "hooks-without-prepare"
        empty_hooks.mkdir()
        env["AGENTSTACK_HOOKS_DIR"] = str(empty_hooks)
    env["AGENTSTACK_CODEX_LAUNCH_BINDING"] = "/parent/launch.json"
    env["AGENTSTACK_CODEX_LAUNCH_ID"] = "parent-launch"

    result = _run_preregistered_codex_spawn(
        layout=layout,
        env=env,
        workdir=workdir,
        task_file=task_file,
    )

    assert result.returncode != 0
    tmux_log = Path(env["FAKE_TMUX_LOG"])
    assert not tmux_log.exists() or "\x1cnew-session\x1c" not in tmux_log.read_text(
        encoding="utf-8"
    )
    assert "server-owner-token" not in result.stdout + result.stderr
    if failure_mode == "prepare_failure":
        assert "fresh Codex history binding expectation" in result.stderr
    else:
        assert "canonical Codex registration metadata is missing" in result.stderr
        assert "Re-run agentstack-preregister-child" in result.stderr
        assert "legacy token-only runtime entry" in result.stderr


@pytest.mark.parametrize("valid_sidecar", [True, False], ids=["success", "failure"])
def test_explicit_codex_handoff_is_consumed_only_after_success(
    binding_env, tmp_path: Path, valid_sidecar: bool
) -> None:
    layout = _codex_entrypoint_layout(tmp_path, "source")
    env, workdir = _fake_codex_launch_env(
        tmp_path,
        runtime=binding_env["runtime"],
        project=binding_env["project"],
        layout=layout,
    )
    task_file = tmp_path / "task.md"
    task_file.write_text("Exercise the explicit handoff.", encoding="utf-8")
    handoff = tmp_path / "explicit-token"
    handoff.write_text("server-owner-token", encoding="utf-8")
    handoff.chmod(0o600)
    sidecar = handoff.with_name(handoff.name + ".binding.json")
    if valid_sidecar:
        sidecar.write_text(json.dumps(_registration(binding_env["project"])), encoding="utf-8")
    else:
        sidecar.write_text("{", encoding="utf-8")
    sidecar.chmod(0o600)

    result = _run_preregistered_codex_spawn(
        layout=layout,
        env=env,
        workdir=workdir,
        task_file=task_file,
        handoff=handoff,
    )

    canonical_token = binding_env["runtime"] / f"agent_token_{AGENT}"
    state_path = binding_env["runtime"] / "child-agents" / f"{AGENT}.json"
    if valid_sidecar:
        assert result.returncode == 0, result.stderr
        assert not handoff.exists() and not sidecar.exists()
        assert canonical_token.is_file() and state_path.is_file()
    else:
        assert result.returncode != 0
        assert handoff.is_file() and sidecar.is_file()
        assert not canonical_token.exists() and not state_path.exists()
        tmux_log = Path(env["FAKE_TMUX_LOG"])
        assert not tmux_log.exists() or "\x1cnew-session\x1c" not in tmux_log.read_text(
            encoding="utf-8"
        )


def test_metadata_without_hook_becomes_unconfirmed_after_grace(binding_env) -> None:
    _prepare(binding_env, now=100.0)

    pending = server._codex_history_binding(AGENT, now=109.0)
    unconfirmed = server._codex_history_binding(AGENT, now=111.0)

    assert pending["history_binding"] == "pending"
    assert unconfirmed["history_binding"] == "unconfirmed"
    assert unconfirmed["history_binding_reason_code"] == "hook_not_observed"


def test_future_launch_timestamp_does_not_extend_the_display_grace(binding_env) -> None:
    _prepare(binding_env, now=200.0)

    state = server._codex_history_binding(AGENT, now=100.0)

    assert state["history_binding"] == "unconfirmed"
    assert state["history_binding_reason_code"] == "hook_not_observed"


def test_same_launch_payload_becomes_the_verified_receipt(binding_env) -> None:
    launch_path, launch_id = _prepare(binding_env)

    assert _record(binding_env, launch_path, launch_id) == "bound"
    state = server._codex_history_binding(AGENT, now=200.0)

    assert state["history_binding"] == "bound"
    assert state["transcript_path"] == str(binding_env["transcript"])
    receipt = json.loads(
        (binding_env["runtime"] / "session_index" / f"{AGENT_ID}.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["launch_id"] == launch_id
    assert receipt["provider"] == "codex"
    assert receipt["registered_by"] == AGENT


def test_resume_revalidates_the_same_session_id_and_payload_path(binding_env) -> None:
    launch_path, launch_id = prepare_mod.prepare(
        binding_env["runtime"],
        binding_env["registration"],
        launch_kind="resume",
        history_mode="enabled",
        now=100.0,
    )

    assert _record(
        binding_env, launch_path, launch_id, source="resume"
    ) == "bound"
    receipt = json.loads(
        (binding_env["runtime"] / "session_index" / f"{AGENT_ID}.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["launch_kind"] == "resume"
    assert receipt["source"] == "resume"
    assert receipt["session_id"] == SESSION_ID


def test_receipt_program_must_match_the_current_registration(binding_env) -> None:
    launch_path, launch_id = _prepare(binding_env)
    assert _record(binding_env, launch_path, launch_id) == "bound"
    receipt_path = binding_env["runtime"] / "session_index" / f"{AGENT_ID}.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["program"] = "codex-cli"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    state = server._codex_history_binding(AGENT, now=200.0)

    assert state["history_binding"] == "unconfirmed"
    assert state["history_binding_reason_code"] == "receipt_missing"


def test_old_valid_index_is_not_success_for_a_new_launch(binding_env) -> None:
    first_path, first_id = _prepare(binding_env, now=100.0)
    assert _record(binding_env, first_path, first_id) == "bound"

    _prepare(binding_env, now=200.0)

    assert server._codex_transcript_path(AGENT) is None
    state = server._codex_history_binding(AGENT, now=211.0)
    assert state["history_binding"] == "unconfirmed"


def test_write_failure_does_not_raise_and_is_visible(binding_env, monkeypatch) -> None:
    launch_path, launch_id = _prepare(binding_env)
    original = record_mod._atomic_json

    def fail_receipt(path: Path, payload: dict) -> None:
        if path.parent.name == "session_index":
            raise OSError("fixture: full disk")
        original(path, payload)

    monkeypatch.setattr(record_mod, "_atomic_json", fail_receipt)

    assert _record(binding_env, launch_path, launch_id) == "write_failed"
    state = server._codex_history_binding(AGENT, now=200.0)
    assert state["history_binding"] == "unconfirmed"
    assert state["history_binding_reason_code"] == "write_failed"


def test_write_failure_cannot_leave_an_old_receipt_authoritative(
    binding_env, monkeypatch
) -> None:
    launch_path, launch_id = _prepare(binding_env)
    assert _record(binding_env, launch_path, launch_id) == "bound"
    receipt_path = binding_env["runtime"] / "session_index" / f"{AGENT_ID}.json"
    old_receipt = receipt_path.read_text(encoding="utf-8")
    original = record_mod._atomic_json

    def fail_replacement(path: Path, payload: dict) -> None:
        if path == receipt_path:
            raise OSError("fixture: receipt replacement failed")
        original(path, payload)

    monkeypatch.setattr(record_mod, "_atomic_json", fail_replacement)

    assert _record(
        binding_env, launch_path, launch_id, source="compact"
    ) == "write_failed"
    assert receipt_path.read_text(encoding="utf-8") == old_receipt
    state = server._codex_history_binding(AGENT, now=200.0)
    assert state["history_binding"] == "unconfirmed"
    assert state["history_binding_reason_code"] == "write_failed"


def test_receipt_commit_is_success_even_if_diagnostic_update_fails(
    binding_env, monkeypatch
) -> None:
    launch_path, launch_id = _prepare(binding_env)
    original = record_mod._atomic_json

    def fail_bound_hint(path: Path, payload: dict) -> None:
        if path == launch_path and payload.get("last_reason") == "bound":
            raise OSError("fixture: diagnostic metadata is not writable")
        original(path, payload)

    monkeypatch.setattr(record_mod, "_atomic_json", fail_bound_hint)

    assert _record(binding_env, launch_path, launch_id) == "bound"
    assert server._codex_history_binding(AGENT, now=200.0)["history_binding"] == "bound"


def test_launch_expectation_atomic_failure_never_reports_a_new_generation(
    binding_env, monkeypatch
) -> None:
    launch_path, launch_id = _prepare(binding_env)
    assert _record(binding_env, launch_path, launch_id) == "bound"
    original = prepare_mod._atomic_json

    def fail_launch(path: Path, payload: dict) -> None:
        raise OSError("fixture: launch metadata is not writable")

    monkeypatch.setattr(prepare_mod, "_atomic_json", fail_launch)
    with pytest.raises(OSError):
        _prepare(binding_env, now=200.0)
    monkeypatch.setattr(prepare_mod, "_atomic_json", original)

    # The old run remains internally consistent; callers must abort before a
    # new CLI starts because no new generation was returned.
    assert server._codex_history_binding(AGENT, now=211.0)["history_binding"] == "bound"
    assert _record(binding_env, launch_path, launch_id) == "bound"


def test_launch_lock_open_failure_never_reports_a_new_generation(
    binding_env, monkeypatch
) -> None:
    launch_path, launch_id = _prepare(binding_env)
    assert _record(binding_env, launch_path, launch_id) == "bound"
    original_open = prepare_mod.os.open

    def fail_lock(path, *args, **kwargs):
        if str(path).endswith(".lock"):
            raise OSError("fixture: launch lock cannot be opened")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(prepare_mod.os, "open", fail_lock)
        with pytest.raises(OSError):
            _prepare(binding_env, now=200.0)

    assert server._codex_history_binding(AGENT, now=211.0)["history_binding"] == "bound"


def test_late_old_callback_cannot_replace_current_receipt(binding_env) -> None:
    launch_path, old_id = _prepare(binding_env, now=100.0)
    current_path, current_id = _prepare(binding_env, now=200.0)
    assert launch_path == current_path
    assert _record(binding_env, current_path, current_id) == "bound"

    other = binding_env["transcript"].with_name("old.jsonl")
    _rollout(other, "old-session-id")
    late = record_mod.record_payload(
        _payload(other, session_id="old-session-id"),
        launch_path=launch_path,
        launch_id=old_id,
    )

    assert late == "stale_launch"
    receipt = json.loads(
        (binding_env["runtime"] / "session_index" / f"{AGENT_ID}.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["launch_id"] == current_id
    assert receipt["session_id"] == SESSION_ID


def test_inherited_same_launch_cannot_bind_a_second_cli_process(binding_env) -> None:
    launch_path, launch_id = _prepare(binding_env)
    assert _record(binding_env, launch_path, launch_id) == "bound"
    other = binding_env["transcript"].with_name("inherited-child.jsonl")
    _rollout(other, "nested-cli-session-id")

    result = record_mod.record_payload(
        _payload(other, session_id="nested-cli-session-id", source="startup"),
        launch_path=launch_path,
        launch_id=launch_id,
    )

    assert result == "id_mismatch"
    state = server._codex_history_binding(AGENT, now=200.0)
    assert state["history_binding"] == "unconfirmed"
    assert state["history_binding_reason_code"] == "id_mismatch"


def test_conflicting_session_stays_rejected_after_receipt_is_removed(binding_env) -> None:
    launch_path, launch_id = _prepare(binding_env)
    assert _record(binding_env, launch_path, launch_id) == "bound"
    other = binding_env["transcript"].with_name("conflicting-session.jsonl")
    _rollout(other, "conflicting-session-id")
    conflict = _payload(other, session_id="conflicting-session-id", source="startup")

    assert record_mod.record_payload(
        conflict, launch_path=launch_path, launch_id=launch_id
    ) == "id_mismatch"
    assert not (
        binding_env["runtime"] / "session_index" / f"{AGENT_ID}.json"
    ).exists()
    for source in ("startup", "compact"):
        conflict["source"] = source
        assert record_mod.record_payload(
            conflict, launch_path=launch_path, launch_id=launch_id
        ) == "id_mismatch"
    assert server._codex_history_binding(AGENT, now=200.0)["history_binding"] == "unconfirmed"


def test_clear_cannot_switch_a_claimed_launch_to_another_session(binding_env) -> None:
    launch_path, launch_id = _prepare(binding_env)
    assert _record(binding_env, launch_path, launch_id) == "bound"
    other = binding_env["transcript"].with_name("clear-session.jsonl")
    _rollout(other, "clear-session-id")

    assert record_mod.record_payload(
        _payload(other, session_id="clear-session-id", source="clear"),
        launch_path=launch_path,
        launch_id=launch_id,
    ) == "id_mismatch"
    assert server._codex_history_binding(AGENT, now=200.0)["history_binding"] == "unconfirmed"
    # The conflict is terminal for this generation, even if A reports again.
    assert _record(binding_env, launch_path, launch_id, source="compact") == "id_mismatch"


def test_conflict_is_unconfirmed_even_if_the_stale_index_cannot_be_deleted(
    binding_env, monkeypatch
) -> None:
    launch_path, launch_id = _prepare(binding_env)
    assert _record(binding_env, launch_path, launch_id) == "bound"
    receipt_path = binding_env["runtime"] / "session_index" / f"{AGENT_ID}.json"
    old_receipt = receipt_path.read_text(encoding="utf-8")
    other = binding_env["transcript"].with_name("undeletable-conflict.jsonl")
    _rollout(other, "undeletable-session-id")
    original_unlink = Path.unlink

    def fail_receipt_unlink(path: Path, *args, **kwargs) -> None:
        if path == receipt_path:
            raise PermissionError("fixture: stale receipt cannot be deleted")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "unlink", fail_receipt_unlink)
        assert record_mod.record_payload(
            _payload(other, session_id="undeletable-session-id"),
            launch_path=launch_path,
            launch_id=launch_id,
        ) == "id_mismatch"

    assert receipt_path.read_text(encoding="utf-8") == old_receipt
    state = server._codex_history_binding(AGENT, now=200.0)
    assert state["history_binding"] == "unconfirmed"
    assert state["history_binding_reason_code"] == "id_mismatch"


def test_null_transcript_is_unconfirmed_not_disabled(binding_env) -> None:
    launch_path, launch_id = _prepare(binding_env)
    assert _record(binding_env, launch_path, launch_id, transcript_path=None) == "no_transcript"
    state = server._codex_history_binding(AGENT, now=200.0)
    assert state["history_binding"] == "unconfirmed"
    assert state["history_binding_reason_code"] == "no_transcript"


def test_explicit_no_history_launch_is_disabled(binding_env) -> None:
    _prepare(binding_env, history_mode="disabled")
    state = server._codex_history_binding(AGENT, now=200.0)
    assert state["history_binding"] == "disabled"


@pytest.mark.parametrize("hook_event", ["SessionStart", "SubagentStart"])
def test_builtin_subagent_event_is_ignored(binding_env, hook_event: str) -> None:
    launch_path, launch_id = _prepare(binding_env)
    result = _record(
        binding_env,
        launch_path,
        launch_id,
        hook_event_name=hook_event,
        agent_id="builtin-child",
    )
    assert result == "ignored"
    assert not (binding_env["runtime"] / "session_index" / f"{AGENT_ID}.json").exists()


def test_cli_entrypoint_is_fail_open_on_bad_payload(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(
                ROOT
                / "integrations/codex_app/plugin/scripts/record-codex-session-index.py"
            ),
        ],
        input="not json",
        text=True,
        capture_output=True,
        env={
            "AGENTSTACK_CODEX_LAUNCH_BINDING": str(tmp_path / "missing.json"),
            "AGENTSTACK_CODEX_LAUNCH_ID": "missing",
        },
        check=False,
    )
    assert result.returncode == 0


def test_cli_entrypoint_derives_runtime_from_launch_path_not_ambient_env(
    binding_env, tmp_path: Path
) -> None:
    launch_path, launch_id = _prepare(binding_env)
    poisoned_runtime = tmp_path / "parent-runtime"
    result = subprocess.run(
        [
            sys.executable,
            str(
                ROOT
                / "integrations/codex_app/plugin/scripts/record-codex-session-index.py"
            ),
        ],
        input=json.dumps(_payload(binding_env["transcript"])),
        text=True,
        capture_output=True,
        env={
            "AGENTSTACK_CODEX_LAUNCH_BINDING": str(launch_path),
            "AGENTSTACK_CODEX_LAUNCH_ID": launch_id,
            "AGENTSTACK_RUNTIME_DIR": str(poisoned_runtime),
        },
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (
        binding_env["runtime"] / "session_index" / f"{AGENT_ID}.json"
    ).is_file()
    assert not (poisoned_runtime / "session_index" / f"{AGENT_ID}.json").exists()


def test_preregister_receipt_reaches_child_state_without_name_lookup(tmp_path: Path) -> None:
    fake_lib = tmp_path / "register.sh"
    fake_lib.write_text(
        """
ags_mail_load_token() { :; }
ags_has_scientist_suffix() { return 0; }
ags_generate_registration_token() { printf 'sent-token\\n'; }
ags_mcp_call() {
  if [[ "$1" == register_agent ]]; then
    printf '{"id":73,"name":"BoundCodex","registration_token":"owner-token"}\\n'
  else
    printf '{}\\n'
  fi
}
ags_mcp_has_error() { return 1; }
ags_extract_agent_name() { python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])'; }
ags_extract_agent_id() { python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])'; }
ags_extract_registration_token() { python3 -c 'import json,sys; print(json.load(sys.stdin)["registration_token"])'; }
ags_store_registration_token() { :; }
ags_apply_contact_policy() { :; }
""",
        encoding="utf-8",
    )
    token = tmp_path / "token-BoundCodex"
    preregister = subprocess.run(
        [
            str(ROOT / "bin" / "agentstack-preregister-child"),
            "--project-key",
            str(tmp_path / "project"),
            "--name",
            AGENT,
            "--program",
            "codex",
            "--model",
            "gpt-test",
            "--token-file-out",
            str(token),
        ],
        text=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "AGENTSTACK_REGISTER_LIB": str(fake_lib),
            "AGENTSTACK_ENV_FILE": "",
        },
        check=False,
    )
    assert preregister.returncode == 0, preregister.stderr
    sidecar = token.with_name(token.name + ".binding.json")
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {
        "agent_id": AGENT_ID,
        "agent_name": AGENT,
        "project_key": str(tmp_path / "project"),
        "program": "codex",
    }

    runtime = tmp_path / "runtime"
    state_path = _adopt_child_handoff(
        tmp_path,
        runtime=runtime,
        agent_name=AGENT,
        project_key=str(tmp_path / "project"),
        token=token,
    )
    state = json.loads(
        state_path.read_text(encoding="utf-8")
    )
    assert state["agent_id"] == AGENT_ID
    assert state["program"] == "codex"
    assert not token.exists() and not sidecar.exists()

    launch_path, launch_id = prepare_mod.prepare(
        runtime, state, launch_kind="startup", history_mode="enabled", now=100.0
    )
    transcript = tmp_path / "delegate-rollout.jsonl"
    _rollout(transcript)
    assert record_mod.record_payload(
        _payload(transcript), launch_path=launch_path, launch_id=launch_id
    ) == "bound"
    assert (runtime / "session_index" / f"{AGENT_ID}.json").is_file()


def test_deck_new_agent_handoff_reaches_recorder_and_reader(
    binding_env, monkeypatch, tmp_path: Path
) -> None:
    runtime = binding_env["runtime"]
    runtime.mkdir()
    (runtime / "agent_token_Parent").write_text(
        "parent-owner-token", encoding="utf-8"
    )
    launcher = tmp_path / "spawn_child.sh"
    launcher.write_text("#!/bin/bash\n", encoding="utf-8")
    launcher.chmod(0o755)
    launched: list[list[str]] = []

    def mcp(method: str, _args: dict, timeout: int = 15) -> dict:
        del timeout
        if method == "register_agent":
            return {
                "ok": True,
                "data": {
                    "id": AGENT_ID,
                    "name": AGENT,
                    "registration_token": "server-child-token",
                },
            }
        return {"ok": True, "data": {}}

    monkeypatch.setattr(server, "SPAWN_SCRIPT", str(launcher))
    monkeypatch.setattr(server, "HERE", str(tmp_path))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "_spawn_name_status", lambda _name: "available")
    monkeypatch.setattr(server, "_mcp_call", mcp)
    monkeypatch.setattr(server, "_runtime_agent_token", lambda _name: "parent-owner-token")
    with monkeypatch.context() as process_patch:
        process_patch.setattr(
            server.subprocess, "Popen", lambda args, **_kwargs: launched.append(args)
        )
        process_patch.setattr(
            server.subprocess,
            "run",
            lambda *_args, **_kwargs: type("Result", (), {"returncode": 0})(),
        )
        result = server.do_spawn(
            {
                "parent": "Parent",
                "name": AGENT,
                "task": "verify deck binding",
                "dir": str(tmp_path),
                "provider": "codex",
                "model": "gpt-5.6-sol",
                "effort": "high",
            }
        )
    assert result["ok"] is True, result

    token = Path(launched[0][4])
    state_path = _adopt_child_handoff(
        tmp_path,
        runtime=runtime,
        agent_name=AGENT,
        project_key=str(binding_env["project"]),
        token=token,
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["agent_id"] == AGENT_ID
    assert state["program"] == "codex-cli"
    with sqlite3.connect(server.DB_PATH) as connection:
        connection.execute(
            "UPDATE agents SET program='codex-cli' WHERE id=?", (AGENT_ID,)
        )

    launch_path, launch_id = prepare_mod.prepare(
        runtime, state, launch_kind="startup", history_mode="enabled", now=100.0
    )
    assert _record(binding_env, launch_path, launch_id) == "bound"
    assert server._codex_history_binding(AGENT, now=200.0)["history_binding"] == "bound"


def test_deck_badges_are_only_wired_in_the_card_renderer() -> None:
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    card_start = html.index("function bay(a,i)")
    card_end = html.index("\nfunction render()", card_start)
    card = html[card_start:card_end]
    assert "? UNBOUND" in card
    assert "— NO HISTORY" in card
    assert html.count("? UNBOUND") == 1
    assert html.count("— NO HISTORY") == 1
