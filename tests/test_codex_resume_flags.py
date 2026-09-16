"""The dashboard's Codex resume applies the same child launch policy as spawn_child.sh.

Before 2026-09-04 the resume path hardcoded `--ask-for-approval on-request`,
no network flag and only the vault as an extra writable root, so a resumed
agent asked for approval on every command while a freshly spawned one did not.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_teardown import TEST_LABEL_PREFIX  # noqa: E402

import dashboard.server as server


ROOT = Path(__file__).resolve().parents[1]
AGENT = "BoundCodex"
AGENT_ID = 73
OWNER_TOKEN = "server-owner-token"


@pytest.fixture
def policy_env(monkeypatch, tmp_path):
    for name in ("AGENTSTACK_CODEX_CHILD_APPROVAL", "AGENTSTACK_CODEX_NETWORK",
                 "AGENTSTACK_CODEX_ADD_DIRS", "AGENTSTACK_SPAWN_DIRS",
                 "AGENTSTACK_SPAWN_ROOTS", "AGENTSTACK_HOME"):
        monkeypatch.delenv(name, raising=False)
    project = tmp_path / "proj with space"
    project.mkdir()
    monkeypatch.setattr(server, "PROJECT_KEY", str(project))
    monkeypatch.setattr(server, "VAULT", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path, project


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def _child_proxy_config(
    *, agent: str, project: Path, token_file: Path, program: str = "codex"
) -> str:
    quote = lambda value: json.dumps(str(value))
    identity = (
        f"AGENTSTACK_PROXY_AGENT_NAME = {quote(agent)}\n"
        f"AGENTSTACK_PROXY_TOKEN_FILE = {quote(token_file)}\n"
        f"AGENTSTACK_PROXY_PROGRAM = {quote(program)}\n"
        f"AGENTSTACK_PROJECT_KEY = {quote(project)}\n"
    )
    return (
        'profile_marker = "orrery-only"\n\n'
        '[mcp_servers.notion]\n'
        'enabled = false\n\n'
        '[plugins."unrelated@fixture"]\n'
        'enabled = false\n\n'
        '[mcp_servers."orrery-mail"]\n'
        'command = "/fixture/run-mcp.sh"\n'
        'args = []\n\n'
        '[mcp_servers."orrery-mail".env]\n'
        f'{identity}\n'
        '[mcp_servers.agentstack]\n'
        'command = "/fixture/run-mcp.sh"\n'
        'args = []\n\n'
        '[mcp_servers.agentstack.env]\n'
        f'{identity}'
    )


def _seed_child_identity(
    runtime: Path,
    project: Path,
    *,
    state_updates: dict | None = None,
    config_text: str | None = None,
    write_home: bool = True,
) -> tuple[Path, Path, bytes | None]:
    token_file = runtime / f"agent_token_{AGENT}"
    _write_private(token_file, OWNER_TOKEN)
    state = {
        "agent_id": AGENT_ID,
        "agent_name": AGENT,
        "project_key": str(project),
        # Preregistration and the resume bootstrap use these two accepted
        # spellings in the live product; they are one provider family.
        "program": "codex-cli",
        "registration_token": OWNER_TOKEN,
    }
    state.update(state_updates or {})
    state_file = runtime / "child-agents" / f"{AGENT}.json"
    _write_private(state_file, json.dumps(state))
    child_home = runtime / "child-agents" / f"{AGENT}.codex-home"
    before = None
    if write_home:
        child_home.mkdir(parents=True, exist_ok=True)
        config = config_text or _child_proxy_config(
            agent=AGENT,
            project=project,
            token_file=token_file,
        )
        config_path = child_home / "config.toml"
        _write_private(config_path, config)
        before = config_path.read_bytes()
    return child_home, token_file, before


def _invoke_resume_entry(monkeypatch, tmp_path, project, runtime):
    rollout = tmp_path / "rollout-session.jsonl"
    rollout.write_text(
        json.dumps({
            "type": "session_meta",
            "payload": {
                "id": "01d13f58-8e1a-7777-a3d8-e2ba243cdb49",
                "cwd": str(project),
            },
        }) + "\n",
        encoding="utf-8",
    )
    install_home = tmp_path / "installed-agentstack"
    bootstrap = install_home / "bin" / "agentstack-codex-bootstrap"
    bootstrap.parent.mkdir(parents=True, exist_ok=True)
    bootstrap.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    registration = {
        "agent_id": AGENT_ID,
        "agent_name": AGENT,
        "project_key": str(project),
        "program": "codex",
    }
    launched = []
    monkeypatch.setenv("AGENTSTACK_HOME", str(install_home))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "_codex_transcript_path", lambda _name: str(rollout))
    monkeypatch.setattr(server, "_codex_registration", lambda _name: registration)
    monkeypatch.setattr(
        server,
        "_open_terminal_tmux",
        lambda args, **kwargs: launched.append(args) or {"ok": True, "adapter": "fixture"},
    )
    return server._do_resume_codex(AGENT), launched


def test_resume_defaults_to_never_with_network_and_project_root(policy_env):
    tmp_path, project = policy_env
    flags = server._codex_child_launch_flags()
    assert flags.startswith("--sandbox workspace-write --ask-for-approval never")
    assert "-c sandbox_workspace_write.network_access=true" in flags
    assert f"--add-dir {shlex.quote(os.path.realpath(str(project)))}" in flags
    # The old hardcoded on-request is gone.
    assert "on-request" not in flags


def test_resume_honours_installer_settings_and_extra_roots(policy_env, monkeypatch):
    tmp_path, project = policy_env
    preset = tmp_path / "code"
    extra = tmp_path / "extra"
    preset.mkdir()
    extra.mkdir()
    monkeypatch.setenv("AGENTSTACK_CODEX_CHILD_APPROVAL", "on-failure")
    monkeypatch.setenv("AGENTSTACK_CODEX_NETWORK", "off")
    monkeypatch.setenv("AGENTSTACK_SPAWN_DIRS", f"{preset}:/does/not/exist")
    monkeypatch.setenv("AGENTSTACK_CODEX_ADD_DIRS", str(extra))
    flags = server._codex_child_launch_flags()
    assert "--ask-for-approval on-failure" in flags
    assert "network_access" not in flags
    dirs = server._codex_child_add_dirs()
    assert dirs[0] == os.path.realpath(str(project))
    assert os.path.realpath(str(preset)) in dirs
    assert dirs[-1] == os.path.realpath(str(extra))
    assert "/does/not/exist" not in dirs
    assert len(dirs) == len(set(dirs))


def test_resume_sources_the_installed_product_bootstrap(policy_env, monkeypatch):
    tmp_path, project = policy_env
    rollout = tmp_path / "rollout-session.jsonl"
    session_id = "01d13f58-8e1a-7777-a3d8-e2ba243cdb49"
    rollout.write_text(
        json.dumps({
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": str(project)},
        }) + "\n",
        encoding="utf-8",
    )
    install_home = tmp_path / "installed-agentstack"
    bootstrap = install_home / "bin" / "agentstack-codex-bootstrap"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    launched = []
    runtime = tmp_path / "runtime"
    custom_home = tmp_path / "custom-codex-home"
    custom_home.mkdir()
    monkeypatch.setenv("AGENTSTACK_HOME", str(install_home))
    monkeypatch.setenv("CODEX_HOME", str(custom_home))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "_codex_transcript_path", lambda _name: str(rollout))
    monkeypatch.setattr(
        server,
        "_codex_registration",
        lambda _name: {
            "agent_id": AGENT_ID,
            "agent_name": AGENT,
            "project_key": str(project),
            "program": "codex",
        },
    )
    monkeypatch.setattr(
        server,
        "_open_terminal_tmux",
        lambda args, **kwargs: launched.append(args) or {"ok": True, "adapter": "fixture"},
    )

    result = server._do_resume_codex("BoundCodex")

    assert result["ok"] is True
    assert len(launched) == 1
    inner = launched[0][-1]
    assert f"source {shlex.quote(str(bootstrap))}" in inner
    assert "AGENTSTACK_CODEX_LAUNCH_KIND=resume" in inner
    assert "export CODEX_HOME=" not in inner
    assert "export CODEX_SHARED_CODEX_DIR=" not in inner
    assert ".codex/bin/codex_agent_bootstrap.sh" not in inner


def test_deck_resume_exec_receives_the_fresh_launch_pair(policy_env, monkeypatch):
    tmp_path, project = policy_env
    zsh = shutil.which("zsh")
    if not zsh:
        pytest.skip("requires zsh to exercise the macOS login-shell boundary")

    session_id = "01d13f58-8e1a-7777-a3d8-e2ba243cdb49"
    rollout = tmp_path / "rollout-session.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": str(project)},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    install_home = tmp_path / "installed-agentstack"
    bindir = install_home / "bin"
    libdir = bindir / "lib"
    hooks = install_home / "hooks"
    libdir.mkdir(parents=True)
    hooks.mkdir(parents=True)
    shutil.copy2(ROOT / "bin" / "agentstack-codex-bootstrap", bindir)
    shutil.copy2(ROOT / "hooks" / "prepare-codex-session-binding.py", hooks)
    (libdir / "agentstack-register.sh").write_text(
        "ags_mail_load_token() { :; }\n"
        "ags_mcp_call() { return 0; }\n"
        "ags_start_mail_watcher() { :; }\n"
        "ags_register_session() {\n"
        "  AGS_REGISTERED_AGENT_NAME=BoundCodex\n"
        "  AGS_REGISTERED_AGENT_ID=73\n"
        "  return 0\n"
        "}\n"
        "ags_record_managed_agent() { :; }\n",
        encoding="utf-8",
    )

    home = tmp_path / "home"
    fake_bin = home / ".local" / "bin"
    fake_bin.mkdir(parents=True)
    default_codex_home = home / ".codex"
    shared_sessions = default_codex_home / "sessions"
    shared_sessions.mkdir(parents=True)
    default_config = default_codex_home / "config.toml"
    _write_private(
        default_config,
        '[mcp_servers."orrery-mail"]\n'
        'url = "http://127.0.0.1:18765/mcp"\n'
        'bearer_token_env_var = "MCP_AGENT_MAIL_TOKEN"\n',
    )
    default_config_before = default_config.read_bytes()
    zdotdir = tmp_path / "zdotdir"
    zdotdir.mkdir()
    runtime = tmp_path / "runtime"
    codex_home, _token_file, config_before = _seed_child_identity(runtime, project)
    (codex_home / "sessions").symlink_to(shared_sessions, target_is_directory=True)
    capture = tmp_path / "codex-env.txt"
    release = tmp_path / "release-hook"
    hook_payload = tmp_path / "session-start.json"
    hook_payload.write_text(
        json.dumps(
            {
                "hook_event_name": "SessionStart",
                "source": "resume",
                "session_id": session_id,
                "transcript_path": str(rollout),
                "cwd": str(project),
                "model": "gpt-5.6-terra",
                "permission_mode": "dontAsk",
            }
        ),
        encoding="utf-8",
    )

    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "if [[ \"${1:-}\" == new-session ]]; then\n"
        "  shift\n"
        "  session= cwd=\n"
        "  while [[ $# -gt 0 ]]; do\n"
        "    case \"$1\" in\n"
        "      -A) shift ;;\n"
        "      -s) session=$2; shift 2 ;;\n"
        "      -c) cwd=$2; shift 2 ;;\n"
        "      *) break ;;\n"
        "    esac\n"
        "  done\n"
        "  export TMUX=/tmp/fake-tmux TMUX_PANE=%0 FAKE_TMUX_SESSION=$session\n"
        "  cd \"$cwd\"\n"
        "  exec \"$@\"\n"
        "fi\n"
        "case \"${1:-}\" in\n"
        "  display-message) printf '%s\\n' \"$FAKE_TMUX_SESSION\" ;;\n"
        "  has-session) exit 1 ;;\n"
        "  rename-session) exit 1 ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' \"${AGENTSTACK_CODEX_LAUNCH_BINDING:-}\" "
        "\"${AGENTSTACK_CODEX_LAUNCH_ID:-}\" \"${AGENTSTACK_CODEX_LAUNCH_KIND:-}\" "
        "\"${CODEX_HOME:-}\" \"${CODEX_SHARED_CODEX_DIR:-}\" \"$*\" "
        "> \"$FAKE_CODEX_CAPTURE\"\n"
        "[[ \"${CODEX_HOME:-}\" == \"$EXPECTED_CODEX_HOME\" ]] || exit 0\n"
        "[[ \"${CODEX_SHARED_CODEX_DIR:-}\" == \"$EXPECTED_CODEX_HOME\" ]] || exit 0\n"
        "[[ \"$(cd \"$CODEX_HOME/sessions\" && pwd -P)\" == "
        "\"$EXPECTED_SHARED_SESSIONS\" ]] || exit 0\n"
        "while [[ ! -f \"$FAKE_CODEX_RELEASE\" ]]; do /bin/sleep 0.01; done\n"
        "\"$FAKE_HOOK_RUNNER\" < \"$FAKE_HOOK_PAYLOAD\"\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)
    fake_codex.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ZDOTDIR", str(zdotdir))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("PATH", f"{fake_bin}:/usr/bin:/bin")
    monkeypatch.setenv("AGENTSTACK_HOME", str(install_home))
    monkeypatch.setenv("AGENTSTACK_PROJECT_KEY", str(project))
    monkeypatch.setenv("AGENTSTACK_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("AGENTSTACK_HOOKS_DIR", str(hooks))
    monkeypatch.setenv("AGENTSTACK_PYTHON", sys.executable)
    monkeypatch.setenv("AGENTSTACK_LABEL_PREFIX", TEST_LABEL_PREFIX)
    monkeypatch.setenv("AGENTSTACK_CHILD_SHELL", zsh)
    monkeypatch.setenv("AGENTSTACK_CODEX_LAUNCH_BINDING", "/parent/launch.json")
    monkeypatch.setenv("AGENTSTACK_CODEX_LAUNCH_ID", "parent-launch")
    monkeypatch.setenv("FAKE_CODEX_CAPTURE", str(capture))
    monkeypatch.setenv("FAKE_CODEX_RELEASE", str(release))
    monkeypatch.setenv("EXPECTED_CODEX_HOME", str(codex_home))
    monkeypatch.setenv("EXPECTED_SHARED_SESSIONS", str(shared_sessions.resolve()))
    monkeypatch.setenv(
        "FAKE_HOOK_RUNNER",
        str(ROOT / "integrations" / "codex_app" / "plugin" / "scripts" / "run-hook.sh"),
    )
    monkeypatch.setenv("FAKE_HOOK_PAYLOAD", str(hook_payload))

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
            "INSERT INTO agents VALUES (73, 1, 'BoundCodex', 'codex', "
            "'2026-09-16 00:00:00')"
        )
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "SESSION_INDEX_DIR", str(runtime / "session_index"))
    monkeypatch.setattr(server, "CODEX_LAUNCH_DIR", str(runtime / "codex_launches"))
    monkeypatch.setattr(server, "_codex_transcript_path", lambda _name: str(rollout))

    old_prepare = subprocess.run(
        [
            sys.executable,
            str(hooks / "prepare-codex-session-binding.py"),
            "--runtime-dir",
            str(runtime),
            "--agent-id",
            "73",
            "--agent-name",
            "BoundCodex",
            "--project-key",
            str(project),
            "--program",
            "codex",
            "--launch-kind",
            "startup",
            "--history-mode",
            "enabled",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert old_prepare.returncode == 0, old_prepare.stderr
    old_launch_path, old_launch_id = old_prepare.stdout.strip().split("\t")
    old_environment = os.environ.copy()
    old_environment.update(
        {
            "AGENTSTACK_CODEX_LAUNCH_BINDING": old_launch_path,
            "AGENTSTACK_CODEX_LAUNCH_ID": old_launch_id,
        }
    )
    old_payload = json.loads(hook_payload.read_text(encoding="utf-8"))
    old_payload["source"] = "startup"
    old_record = subprocess.run(
        [
            sys.executable,
            str(
                ROOT
                / "integrations"
                / "codex_app"
                / "plugin"
                / "scripts"
                / "record-codex-session-index.py"
            ),
        ],
        input=json.dumps(old_payload),
        env=old_environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert old_record.returncode == 0, old_record.stderr
    assert server._codex_history_binding("BoundCodex")["history_binding"] == "bound"

    processes = []

    def fake_terminal(tmux_args, **_kwargs):
        process = subprocess.Popen(
            [str(fake_tmux), *tmux_args[1:]],
            env=os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        processes.append(process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not capture.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                return {
                    "ok": False,
                    "adapter": "fixture",
                    "error": stderr or stdout,
                }
            time.sleep(0.01)
        return {
            "ok": capture.exists(),
            "adapter": "fixture",
            "error": "fake Codex did not start",
        }

    monkeypatch.setattr(server, "_open_terminal_tmux", fake_terminal)

    try:
        result = server._do_resume_codex("BoundCodex")

        assert result["ok"] is True, result
        after_grace = time.time() + server.CODEX_BINDING_GRACE_SECONDS + 1
        assert server._codex_history_binding(
            "BoundCodex", now=after_grace
        )["history_binding"] == "unconfirmed"
        (
            launch_path,
            launch_id,
            launch_kind,
            observed_codex_home,
            observed_shared_home,
            args,
        ) = capture.read_text(encoding="utf-8").rstrip("\n").split("\t", 5)
        assert launch_path == str(runtime / "codex_launches" / "73.json")
        assert launch_id and launch_id != "parent-launch"
        assert launch_kind == "resume"
        assert observed_codex_home == str(codex_home)
        assert observed_shared_home == str(codex_home)
        assert args.startswith(f"resume {session_id} -C {project}")
        assert f"--add-dir {codex_home}" in args
        launch = json.loads(Path(launch_path).read_text(encoding="utf-8"))
        assert launch["launch_id"] == launch_id
        assert launch["claimed_session_id"] is None
        release.write_text("continue\n", encoding="utf-8")
        stdout, stderr = processes[0].communicate(timeout=10)
        assert processes[0].returncode == 0, {"stdout": stdout, "stderr": stderr}
        assert server._codex_history_binding("BoundCodex")["history_binding"] == "bound"
        assert (codex_home / "config.toml").read_bytes() == config_before
        assert default_config.read_bytes() == default_config_before
        child_config = tomllib.loads(
            (codex_home / "config.toml").read_text(encoding="utf-8")
        )
        assert child_config["profile_marker"] == "orrery-only"
        assert child_config["mcp_servers"]["notion"]["enabled"] is False
        assert child_config["plugins"]["unrelated@fixture"]["enabled"] is False
        assert (codex_home / "sessions").resolve() == shared_sessions.resolve()
    finally:
        # An assertion before the normal release must not leave the fake Codex
        # spinning in its first-submit wait loop.
        release.write_text("continue\n", encoding="utf-8")
        for process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


def test_resume_child_home_rejects_orphan_without_state(policy_env, monkeypatch):
    tmp_path, project = policy_env
    runtime = tmp_path / "runtime"
    orphan = runtime / "child-agents" / f"{AGENT}.codex-home"
    orphan.mkdir(parents=True)
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    registration = {
        "agent_id": AGENT_ID,
        "agent_name": AGENT,
        "project_key": str(project),
        "program": "codex",
    }

    with pytest.raises(ValueError, match="no matching child state"):
        server._codex_resume_child_home(AGENT, registration)


@pytest.mark.parametrize(
    ("case", "state_updates", "config_factory", "expected"),
    [
        ("state-project", {"project_key": "/another/project"}, None,
         "state belongs to another registration"),
        ("state-id", {"agent_id": 999}, None,
         "state belongs to another registration"),
        ("state-name", {"agent_name": "OtherAgent"}, None,
         "state belongs to another registration"),
        ("state-token", {"registration_token": "older-owner-token"}, None,
         "state and credential are from different registrations"),
        ("config-agent", None,
         lambda project, token: _child_proxy_config(
             agent="OtherAgent", project=project, token_file=token),
         "proxy belongs to another registration"),
        ("config-project", None,
         lambda _project, token: _child_proxy_config(
             agent=AGENT, project=Path("/another/project"), token_file=token),
         "proxy belongs to another registration"),
        ("config-program", None,
         lambda project, token: _child_proxy_config(
             agent=AGENT, project=project, token_file=token, program="claude-code"),
         "proxy belongs to another registration"),
        ("config-token", None,
         lambda project, _token: _child_proxy_config(
             agent=AGENT, project=project, token_file=Path("/tmp/other-token")),
         "proxy belongs to another registration"),
        ("direct-http", None,
         lambda _project, _token: (
             '[mcp_servers."orrery-mail"]\n'
             'url = "http://127.0.0.1:18765/mcp"\n'
             'bearer_token_env_var = "MCP_AGENT_MAIL_TOKEN"\n'
         ),
         "Mail alias is not a local proxy"),
    ],
)
def test_resume_child_home_rejects_identity_or_proxy_mismatch(
    policy_env, monkeypatch, case, state_updates, config_factory, expected
):
    tmp_path, project = policy_env
    runtime = tmp_path / f"runtime-{case}"
    token_file = runtime / f"agent_token_{AGENT}"
    config_text = config_factory(project, token_file) if config_factory else None
    _seed_child_identity(
        runtime,
        project,
        state_updates=state_updates,
        config_text=config_text,
    )
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    registration = {
        "agent_id": AGENT_ID,
        "agent_name": AGENT,
        "project_key": str(project),
        "program": "codex",
    }

    with pytest.raises(ValueError, match=expected):
        server._codex_resume_child_home(AGENT, registration)


@pytest.mark.parametrize(
    "case", ["state-program-list", "config-program-list", "nonascii-token"]
)
def test_resume_entry_rejects_malformed_metadata_before_terminal(
    policy_env, monkeypatch, case
):
    tmp_path, project = policy_env
    runtime = tmp_path / f"runtime-{case}"
    state_updates = None
    config_text = None
    secret = ""
    if case == "state-program-list":
        state_updates = {"program": ["codex"]}
    elif case == "config-program-list":
        token_file = runtime / f"agent_token_{AGENT}"
        config_text = _child_proxy_config(
            agent=AGENT, project=project, token_file=token_file
        ).replace(
            'AGENTSTACK_PROXY_PROGRAM = "codex"',
            'AGENTSTACK_PROXY_PROGRAM = ["codex"]',
        )
    else:
        secret = "所有者資格情報"
        state_updates = {"registration_token": secret}
    _seed_child_identity(
        runtime,
        project,
        state_updates=state_updates,
        config_text=config_text,
    )

    result, launched = _invoke_resume_entry(monkeypatch, tmp_path, project, runtime)

    assert result["ok"] is False
    assert "Codex child 設定を確認できません" in result["error"]
    if secret:
        assert secret not in result["error"]
    assert launched == []


def test_resume_entry_rejects_symlinked_child_config_before_terminal(
    policy_env, monkeypatch
):
    tmp_path, project = policy_env
    runtime = tmp_path / "runtime-symlink-config"
    child_home, _token_file, _before = _seed_child_identity(runtime, project)
    config_path = child_home / "config.toml"
    outside = tmp_path / "outside-config.toml"
    outside.write_bytes(config_path.read_bytes())
    outside.chmod(0o600)
    config_path.unlink()
    config_path.symlink_to(outside)

    result, launched = _invoke_resume_entry(monkeypatch, tmp_path, project, runtime)

    assert result["ok"] is False
    assert "Codex child config is not a regular file" in result["error"]
    assert launched == []


def test_resume_valid_state_without_home_uses_default_with_visible_warning(
    policy_env, monkeypatch, caplog
):
    tmp_path, project = policy_env
    runtime = tmp_path / "runtime"
    _seed_child_identity(runtime, project, write_home=False)
    rollout = tmp_path / "rollout-session.jsonl"
    session_id = "01d13f58-8e1a-7777-a3d8-e2ba243cdb49"
    rollout.write_text(
        json.dumps({
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": str(project)},
        }) + "\n",
        encoding="utf-8",
    )
    install_home = tmp_path / "installed-agentstack"
    bootstrap = install_home / "bin" / "agentstack-codex-bootstrap"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    launched = []
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    registration = {
        "agent_id": AGENT_ID,
        "agent_name": AGENT,
        "project_key": str(project),
        "program": "codex",
    }
    monkeypatch.setenv("AGENTSTACK_HOME", str(install_home))
    monkeypatch.setattr(server, "_codex_transcript_path", lambda _name: str(rollout))
    monkeypatch.setattr(server, "_codex_registration", lambda _name: registration)
    monkeypatch.setattr(
        server,
        "_open_terminal_tmux",
        lambda args, **kwargs: launched.append(args) or {"ok": True, "adapter": "fixture"},
    )

    with caplog.at_level("WARNING"):
        result = server._do_resume_codex(AGENT)

    assert result["ok"] is True
    assert "子専用設定なし" in result["detail"]
    assert "Mail 接続は未確認" in result["detail"]
    assert "child home absent" in caplog.text
    assert f"agent={AGENT} id={AGENT_ID}" in caplog.text
    assert len(launched) == 1
    assert "export CODEX_HOME=" not in launched[0][-1]
    assert "export CODEX_SHARED_CODEX_DIR=" not in launched[0][-1]


def test_installed_bootstrap_creates_a_fresh_resume_generation(policy_env):
    tmp_path, project = policy_env
    install_home = tmp_path / "installed-agentstack"
    bindir = install_home / "bin"
    libdir = bindir / "lib"
    hooks = install_home / "hooks"
    libdir.mkdir(parents=True)
    hooks.mkdir(parents=True)
    bootstrap = bindir / "agentstack-codex-bootstrap"
    bootstrap.write_text(
        (ROOT / "bin" / "agentstack-codex-bootstrap").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    prepare = hooks / "prepare-codex-session-binding.py"
    prepare.write_text(
        (ROOT / "hooks" / "prepare-codex-session-binding.py").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    (libdir / "agentstack-register.sh").write_text(
        "ags_mail_load_token() { :; }\n"
        "ags_mcp_call() { return 0; }\n"
        "ags_start_mail_watcher() { :; }\n"
        "ags_register_session() {\n"
        "  AGS_REGISTERED_AGENT_NAME=BoundCodex\n"
        "  AGS_REGISTERED_AGENT_ID=73\n"
        "  return 0\n"
        "}\n"
        "ags_record_managed_agent() { :; }\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    command = (
        "AGENTSTACK_CODEX_LAUNCH_BINDING=/parent/launch.json; "
        "AGENTSTACK_CODEX_LAUNCH_ID=parent-launch; "
        f'source "{bootstrap}" "{project}" >/dev/null; '
        "status=$?; printf '%s|%s|%s\n' \"$status\" "
        '"$AGENTSTACK_CODEX_LAUNCH_BINDING" "$AGENTSTACK_CODEX_LAUNCH_ID"'
    )
    result = subprocess.run(
        ["bash", "-c", command],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path / "clean-home"),
            "AGENTSTACK_PROJECT_KEY": str(project),
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_HOOKS_DIR": str(hooks),
            "AGENTSTACK_RESERVED_IDENTITY": "1",
            "AGENTSTACK_CODEX_LAUNCH_KIND": "resume",
            "AGENT_NAME": "BoundCodex",
            "TMUX": "",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    status, launch_path, launch_id = result.stdout.strip().split("|")
    assert status == "0", result.stderr
    assert launch_path == str(runtime / "codex_launches" / "73.json")
    assert launch_id and launch_id != "parent-launch"
    launch = json.loads(Path(launch_path).read_text(encoding="utf-8"))
    assert launch["launch_kind"] == "resume"
    assert launch["agent_id"] == 73
    assert launch["claimed_session_id"] is None
    assert launch["receipt_id"] is None


@pytest.mark.parametrize("failure_mode", ["project_unset", "health_unreachable"])
def test_reserved_resume_stops_before_exec_when_binding_preconditions_fail(
    policy_env, failure_mode
):
    tmp_path, project = policy_env
    install_home = tmp_path / "installed-agentstack"
    bindir = install_home / "bin"
    libdir = bindir / "lib"
    libdir.mkdir(parents=True)
    bootstrap = bindir / "agentstack-codex-bootstrap"
    bootstrap.write_text(
        (ROOT / "bin" / "agentstack-codex-bootstrap").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    health_status = 1 if failure_mode == "health_unreachable" else 0
    (libdir / "agentstack-register.sh").write_text(
        "ags_mail_load_token() { :; }\n"
        f"ags_mcp_call() {{ return {health_status}; }}\n"
        "ags_start_mail_watcher() { :; }\n"
        "ags_record_managed_agent() { :; }\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    command = (
        f'source "{bootstrap}" "{project}" >/dev/null '
        "&& printf REACHED_CODEX_EXEC_BRANCH"
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "clean-home"),
        "AGENTSTACK_PROJECT_KEY": (
            "" if failure_mode == "project_unset" else str(project)
        ),
        "AGENTSTACK_RUNTIME_DIR": str(runtime),
        "AGENTSTACK_RESERVED_IDENTITY": "1",
        "AGENTSTACK_CODEX_LAUNCH_KIND": "resume",
        "AGENT_NAME": "BoundCodex",
        "TMUX": "",
    }

    result = subprocess.run(
        ["bash", "-c", command],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "REACHED_CODEX_EXEC_BRANCH" not in result.stdout
    assert not (runtime / "codex_launches").exists()
