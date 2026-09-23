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
SESSION_ID = "01d13f58-8e1a-7777-a3d8-e2ba243cdb49"


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
    source_home = tmp_path / ".codex"
    source_home.mkdir()
    _write_private(source_home / "config.toml", 'model = "gpt-5.6-terra"\n')
    return tmp_path, project


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def _seed_bound_receipt(
    runtime: Path,
    project: Path,
    transcript: Path,
    *,
    agent_name: str = AGENT,
    launch_origin: str = "child",
    launch_id: str = "prior-launch",
) -> None:
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": SESSION_ID, "cwd": str(project)},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    receipt = {
        "schema_version": 2,
        "binding_kind": "self",
        "provider": "codex",
        "program": "codex",
        "agent_id": AGENT_ID,
        "agent_name": agent_name,
        "project_key": str(project),
        "registered_by": agent_name,
        "launch_id": launch_id,
        "receipt_id": "prior-receipt",
        "launch_kind": "startup",
        "session_id": SESSION_ID,
        "transcript_path": str(transcript),
        "source": "startup",
        "recorded_at": "2026-09-23T00:00:00+00:00",
        "launch_origin": launch_origin,
    }
    if launch_origin == "child":
        receipt["codex_mcp_profile"] = "orrery-only"
    _write_private(
        runtime / "session_index" / f"{AGENT_ID}.json",
        json.dumps(receipt),
    )


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
        "schema_version": 1,
        "launch_origin": "child",
        "provider": "codex",
        "agent_id": AGENT_ID,
        "agent_name": AGENT,
        "project_key": str(project),
        # Preregistration and the resume bootstrap use these two accepted
        # spellings in the live product; they are one provider family.
        "program": "codex-cli",
        "codex_mcp_profile": "orrery-only",
        "registration_token": OWNER_TOKEN,
        "retired_at": "2026-09-21T00:00:00Z",
        "resume_expires_at": "2999-09-21T00:00:00Z",
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
                "id": SESSION_ID,
                "cwd": str(project),
            },
        }) + "\n",
        encoding="utf-8",
    )
    install_home = tmp_path / "installed-agentstack"
    bootstrap = install_home / "bin" / "agentstack-codex-bootstrap"
    bootstrap.parent.mkdir(parents=True, exist_ok=True)
    bootstrap.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    installed_hooks = install_home / "hooks"
    installed_hooks.mkdir()
    shutil.copy2(ROOT / "hooks" / "child_resume.py", installed_hooks)
    runner = (
        install_home
        / "integrations"
        / "codex_app"
        / "plugin"
        / "scripts"
        / "run-mcp.sh"
    )
    runner.parent.mkdir(parents=True)
    runner.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    runner.chmod(0o755)
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
    worktree_root = tmp_path / "durable-worktrees"
    preset.mkdir()
    extra.mkdir()
    worktree_root.mkdir()
    monkeypatch.setenv("AGENTSTACK_CODEX_CHILD_APPROVAL", "on-failure")
    monkeypatch.setenv("AGENTSTACK_CODEX_NETWORK", "off")
    monkeypatch.setenv("AGENTSTACK_SPAWN_DIRS", f"{preset}:/does/not/exist")
    monkeypatch.setenv("AGENTSTACK_CODEX_ADD_DIRS", str(extra))
    monkeypatch.setenv("AGENTSTACK_WORKTREE_ROOT", str(worktree_root))
    real_isdir = os.path.isdir
    monkeypatch.setattr(
        server.os.path,
        "isdir",
        lambda path: path == "/tmp/cc-worktrees" or real_isdir(path),
    )
    flags = server._codex_child_launch_flags()
    assert "--ask-for-approval on-failure" in flags
    assert "network_access" not in flags
    dirs = server._codex_child_add_dirs()
    assert dirs[0] == os.path.realpath(str(project))
    assert os.path.realpath(str(preset)) in dirs
    assert os.path.realpath(str(worktree_root)) in dirs
    assert os.path.realpath("/tmp/cc-worktrees") in dirs
    assert dirs[-1] == os.path.realpath(str(extra))
    assert "/does/not/exist" not in dirs
    assert len(dirs) == len(set(dirs))


def test_resume_sources_the_installed_product_bootstrap(policy_env, monkeypatch):
    tmp_path, project = policy_env
    rollout = tmp_path / "rollout-session.jsonl"
    session_id = SESSION_ID
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
    hooks = install_home / "hooks"
    hooks.mkdir()
    shutil.copy2(ROOT / "hooks" / "child_resume.py", hooks)
    runner = (
        install_home / "integrations" / "codex_app" / "plugin" / "scripts" / "run-mcp.sh"
    )
    runner.parent.mkdir(parents=True)
    runner.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    runner.chmod(0o755)
    launched = []
    runtime = tmp_path / "runtime"
    custom_home = tmp_path / "custom-codex-home"
    custom_home.mkdir()
    _write_private(custom_home / "config.toml", 'model = "gpt-5.6-terra"\n')
    child_home, _token, _old_config = _seed_child_identity(
        runtime, project, write_home=False
    )
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
    assert f"AGENTSTACK_CODEX_RESUME_SESSION_ID={session_id}" in inner
    assert f"export CODEX_HOME={shlex.quote(str(child_home))}" in inner
    assert f"export CODEX_SHARED_CODEX_DIR={shlex.quote(str(child_home))}" in inner
    assert "AGENTSTACK_CODEX_CHILD_MCP_PROFILE=orrery-only" in inner
    assert (child_home / "config.toml").is_file()
    assert ".codex/bin/codex_agent_bootstrap.sh" not in inner


def test_standalone_resume_uses_current_home_without_child_lifecycle(
    policy_env, monkeypatch
):
    tmp_path, project = policy_env
    runtime = tmp_path / "runtime"
    rollout = tmp_path / "rollout-session.jsonl"
    session_id = SESSION_ID
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
    bootstrap = install_home / "bin" / "agentstack-codex-bootstrap"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    token = runtime / f"agent_token_{AGENT}"
    _write_private(token, OWNER_TOKEN)
    receipt = runtime / "session_index" / f"{AGENT_ID}.json"
    _write_private(
        receipt,
        json.dumps(
            {
                "schema_version": 2,
                "binding_kind": "self",
                "provider": "codex",
                "program": "codex",
                "agent_id": AGENT_ID,
                "agent_name": AGENT,
                "project_key": str(project),
                "registered_by": AGENT,
                "transcript_path": str(rollout),
                "launch_origin": "standalone",
            }
        ),
    )
    launched = []
    monkeypatch.setenv("AGENTSTACK_HOME", str(install_home))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "SESSION_INDEX_DIR", str(runtime / "session_index"))
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

    result = server._do_resume_codex(AGENT)

    assert result["ok"] is True
    inner = launched[0][-1]
    assert "AGENTSTACK_CODEX_LAUNCH_ORIGIN=standalone" in inner
    assert "AGENTSTACK_CODEX_CHILD_MCP_PROFILE" not in inner
    assert "export CODEX_HOME=" not in inner
    assert "cleanup-child-agent.sh" not in inner
    assert "discard-generated" not in inner


def test_deck_resume_exec_receives_the_fresh_launch_pair(policy_env, monkeypatch):
    tmp_path, project = policy_env
    zsh = shutil.which("zsh")
    if not zsh:
        pytest.skip("requires zsh to exercise the macOS login-shell boundary")

    session_id = SESSION_ID
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
    for name in (
        "child_resume.py",
        "cleanup-child-agent.sh",
        "project-context.sh",
        "resolve-agent-name.sh",
    ):
        shutil.copy2(ROOT / "hooks" / name, hooks)
    proxy = (
        install_home / "integrations" / "codex_app" / "plugin" / "scripts" / "run-mcp.sh"
    )
    proxy.parent.mkdir(parents=True)
    proxy.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    proxy.chmod(0o755)
    (libdir / "agentstack-register.sh").write_text(
        "ags_mail_load_token() { :; }\n"
        "ags_mcp_call() { printf '{\"result\":{}}\\n'; }\n"
        "ags_mcp_has_error() { return 1; }\n"
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
        'profile_marker = "current-source"\n\n'
        '[mcp_servers."orrery-mail"]\n'
        'url = "http://127.0.0.1:18765/mcp"\n'
        'bearer_token_env_var = "MCP_AGENT_MAIL_TOKEN"\n\n'
        '[mcp_servers.notion]\n'
        'url = "https://mcp.notion.test/mcp"\n'
        'enabled = true\n\n'
        '[plugins."unrelated@fixture"]\n'
        'enabled = true\n',
    )
    default_config_before = default_config.read_bytes()
    zdotdir = tmp_path / "zdotdir"
    zdotdir.mkdir()
    runtime = tmp_path / "runtime"
    codex_home, _token_file, config_before = _seed_child_identity(runtime, project)
    _write_private(codex_home / "config.toml", 'stale_snapshot = true\n')
    config_before = (codex_home / "config.toml").read_bytes()
    (codex_home / "sessions").symlink_to(shared_sessions, target_is_directory=True)
    capture = tmp_path / "codex-env.txt"
    submit = tmp_path / "submit-prompt"
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
            "      -e) export \"$2\"; shift 2 ;;\n"
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
        "hooked=0\n"
        "while :; do\n"
        "  if [[ \"$hooked\" == 0 && -f \"$FAKE_CODEX_SUBMIT\" ]]; then\n"
        "    \"$FAKE_HOOK_RUNNER\" < \"$FAKE_HOOK_PAYLOAD\"\n"
        "    hooked=1\n"
        "  fi\n"
        "  [[ -f \"$FAKE_CODEX_RELEASE\" ]] && break\n"
        "  /bin/sleep 0.01\n"
        "done\n",
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
    monkeypatch.setenv("AGENTSTACK_MCP_URL", "http://127.0.0.1:1/mcp")
    # server.py resolves service configuration at import time.  Updating only
    # os.environ here made this fixture inherit the caller's import-time mode;
    # under env -i that was the default `auto`, so cleanup correctly stopped
    # before local deletion when no legacy bearer token existed.
    monkeypatch.setattr(server, "MAIL_HTTP_BEARER_MODE", "disabled")
    monkeypatch.setenv("AGENTSTACK_HOOKS_DIR", str(hooks))
    monkeypatch.setenv("AGENTSTACK_PYTHON", sys.executable)
    monkeypatch.setenv("AGENTSTACK_LABEL_PREFIX", TEST_LABEL_PREFIX)
    monkeypatch.setenv("AGENTSTACK_CHILD_SHELL", zsh)
    monkeypatch.setenv("AGENTSTACK_CODEX_LAUNCH_BINDING", "/parent/launch.json")
    monkeypatch.setenv("AGENTSTACK_CODEX_LAUNCH_ID", "parent-launch")
    monkeypatch.setenv("FAKE_CODEX_CAPTURE", str(capture))
    monkeypatch.setenv("FAKE_CODEX_SUBMIT", str(submit))
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
            "--launch-origin",
            "child",
            "--codex-mcp-profile",
            "orrery-only",
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
        )["history_binding"] == "bound"
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
        assert launch["resume_session_id"] == session_id
        assert launch["fallback_launch_id"] == old_launch_id
        assert launch["launch_origin"] == "child"
        assert launch["codex_mcp_profile"] == "orrery-only"
        assert (codex_home / "config.toml").read_bytes() != config_before
        child_config = tomllib.loads(
            (codex_home / "config.toml").read_text(encoding="utf-8")
        )
        assert child_config["profile_marker"] == "current-source"
        assert child_config["mcp_servers"]["notion"]["enabled"] is False
        assert child_config["plugins"]["unrelated@fixture"]["enabled"] is False
        assert (codex_home / "sessions").resolve() == shared_sessions.resolve()
        # Real Codex 0.156 emits SessionStart(resume) only after the first
        # prompt is submitted. The fake keeps the REPL open until then.
        submit.write_text("first prompt\n", encoding="utf-8")
        release.write_text("continue\n", encoding="utf-8")
        stdout, stderr = processes[0].communicate(timeout=10)
        assert processes[0].returncode == 0, {"stdout": stdout, "stderr": stderr}
        assert server._codex_history_binding("BoundCodex")["history_binding"] == "bound"
        assert default_config.read_bytes() == default_config_before
        assert not codex_home.exists()
        retained = json.loads(
            (runtime / "child-agents" / f"{AGENT}.json").read_text(encoding="utf-8")
        )
        assert retained["retired_at"]
        assert retained["resume_expires_at"]
        assert _token_file.is_file()

        # A fresh receipt must retain child provenance, otherwise cleanup after
        # the first resume makes the same row fall back to provenance_missing.
        first_receipt = json.loads(
            (runtime / "session_index" / "73.json").read_text(encoding="utf-8")
        )
        assert first_receipt["launch_origin"] == "child"
        assert first_receipt["codex_mcp_profile"] == "orrery-only"

        default_config.write_text(
            default_config.read_text(encoding="utf-8").replace(
                'profile_marker = "current-source"',
                'profile_marker = "current-source-second"',
            ),
            encoding="utf-8",
        )
        default_config.chmod(0o600)
        capture.unlink()
        release.unlink()
        submit.unlink()
        second = server._do_resume_codex("BoundCodex")
        assert second["ok"] is True, second
        second_launch_path, second_launch_id, *_rest = capture.read_text(
            encoding="utf-8"
        ).rstrip("\n").split("\t", 5)
        assert second_launch_id != launch_id
        assert second_launch_path == launch_path
        second_config = tomllib.loads(
            (codex_home / "config.toml").read_text(encoding="utf-8")
        )
        assert second_config["profile_marker"] == "current-source-second"
        # Exit this resumed REPL without a prompt: no SessionStart receipt is
        # emitted, but the exact prior receipt remains authoritative.
        first_receipt_bytes = (
            runtime / "session_index" / "73.json"
        ).read_bytes()
        release.write_text("continue\n", encoding="utf-8")
        stdout, stderr = processes[1].communicate(timeout=10)
        assert processes[1].returncode == 0, {"stdout": stdout, "stderr": stderr}
        assert (runtime / "session_index" / "73.json").read_bytes() == first_receipt_bytes
        assert server._codex_history_binding("BoundCodex")["history_binding"] == "bound"
        assert not codex_home.exists()

        # The no-prompt exit is still one-click resumable. This third launch
        # proves fallback chaining, then submits a prompt so a fresh receipt
        # replaces the fallback normally.
        default_config.write_text(
            default_config.read_text(encoding="utf-8").replace(
                'profile_marker = "current-source-second"',
                'profile_marker = "current-source-third"',
            ),
            encoding="utf-8",
        )
        default_config.chmod(0o600)
        capture.unlink()
        release.unlink()
        third = server._do_resume_codex("BoundCodex")
        assert third["ok"] is True, third
        third_launch_path, third_launch_id, *_rest = capture.read_text(
            encoding="utf-8"
        ).rstrip("\n").split("\t", 5)
        assert third_launch_path == launch_path
        assert third_launch_id not in {launch_id, second_launch_id}
        third_config = tomllib.loads(
            (codex_home / "config.toml").read_text(encoding="utf-8")
        )
        assert third_config["profile_marker"] == "current-source-third"
        assert server._codex_history_binding("BoundCodex")["history_binding"] == "bound"
        submit.write_text("third prompt\n", encoding="utf-8")
        release.write_text("continue\n", encoding="utf-8")
        stdout, stderr = processes[2].communicate(timeout=10)
        assert processes[2].returncode == 0, {"stdout": stdout, "stderr": stderr}
        third_receipt = json.loads(
            (runtime / "session_index" / "73.json").read_text(encoding="utf-8")
        )
        assert third_receipt["launch_id"] == third_launch_id
        assert third_receipt["launch_origin"] == "child"
        assert third_receipt["codex_mcp_profile"] == "orrery-only"
        assert server._codex_history_binding("BoundCodex")["history_binding"] == "bound"
        assert not codex_home.exists()
    finally:
        # An assertion before the normal release must not leave the fake Codex
        # spinning in its first-submit wait loop.
        submit.write_text("prompt\n", encoding="utf-8")
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

    with pytest.raises(server._ResumeCapabilityError) as raised:
        server._codex_resume_child_home(AGENT, registration)
    assert raised.value.code == "credential_missing"


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
    "case", ["state-program-list", "nonascii-token"]
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
    assert result["resume_capability"] == "identity_mismatch"
    assert result["error"] == server.RESUME_CAPABILITY_MESSAGES["identity_mismatch"]
    if secret:
        assert secret not in result["error"]
    assert launched == []


def test_resume_entry_discards_old_symlinked_child_config_before_terminal(
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

    assert result["ok"] is True
    assert len(launched) == 1
    assert outside.is_file()
    assert config_path.is_file()
    assert not config_path.is_symlink()


def test_resume_tmux_receives_runtime_and_retention_environment(
    policy_env, monkeypatch
):
    tmp_path, project = policy_env
    runtime = tmp_path / "runtime-resume-environment"
    _seed_child_identity(runtime, project)
    monkeypatch.setenv("AGENTSTACK_CHILD_RESUME_RETENTION_DAYS", "17")

    result, launched = _invoke_resume_entry(monkeypatch, tmp_path, project, runtime)

    assert result["ok"] is True
    assert len(launched) == 1
    assert f"AGENTSTACK_RUNTIME_DIR={runtime}" in launched[0]
    assert "AGENTSTACK_CHILD_RESUME_RETENTION_DAYS=17" in launched[0]


def test_resume_valid_state_without_home_regenerates_private_home(
    policy_env, monkeypatch
):
    tmp_path, project = policy_env
    runtime = tmp_path / "runtime"
    _seed_child_identity(runtime, project, write_home=False)
    rollout = tmp_path / "rollout-session.jsonl"
    session_id = SESSION_ID
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
    installed_hooks = install_home / "hooks"
    installed_hooks.mkdir()
    shutil.copy2(ROOT / "hooks" / "child_resume.py", installed_hooks)
    runner = (
        install_home / "integrations" / "codex_app" / "plugin" / "scripts" / "run-mcp.sh"
    )
    runner.parent.mkdir(parents=True)
    runner.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    runner.chmod(0o755)
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

    result = server._do_resume_codex(AGENT)

    assert result["ok"] is True
    assert len(launched) == 1
    child_home = runtime / "child-agents" / f"{AGENT}.codex-home"
    assert (child_home / "config.toml").is_file()
    assert f"export CODEX_HOME={shlex.quote(str(child_home))}" in launched[0][-1]
    assert f"export CODEX_SHARED_CODEX_DIR={shlex.quote(str(child_home))}" in launched[0][-1]


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
    shutil.copy2(ROOT / "hooks" / "child_resume.py", hooks)
    (libdir / "agentstack-register.sh").write_text(
        "ags_mail_load_token() { :; }\n"
        "ags_mcp_call() { printf '{\"result\":{}}\\n'; }\n"
        "ags_mcp_has_error() { return 1; }\n"
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
    _seed_child_identity(runtime, project, write_home=False)
    _seed_bound_receipt(runtime, project, tmp_path / "prior-rollout.jsonl")
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
            "AGENTSTACK_PYTHON": sys.executable,
            "AGENTSTACK_RESERVED_IDENTITY": "1",
            "AGENTSTACK_CODEX_LAUNCH_KIND": "resume",
            "AGENTSTACK_CODEX_RESUME_SESSION_ID": SESSION_ID,
            "AGENTSTACK_CODEX_CHILD_MCP_PROFILE": "orrery-only",
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
    assert launch["resume_session_id"] == SESSION_ID
    assert launch["fallback_launch_id"] == "prior-launch"
    assert launch["fallback_receipt_id"] == "prior-receipt"
    retained = json.loads(
        (runtime / "child-agents" / f"{AGENT}.json").read_text(encoding="utf-8")
    )
    assert retained["resume_in_progress_at"].endswith("Z")


def test_top_level_bootstrap_records_standalone_origin(policy_env):
    tmp_path, project = policy_env
    install_home = tmp_path / "installed-agentstack"
    bindir = install_home / "bin"
    libdir = bindir / "lib"
    hooks = install_home / "hooks"
    libdir.mkdir(parents=True)
    hooks.mkdir(parents=True)
    bootstrap = bindir / "agentstack-codex-bootstrap"
    shutil.copy2(ROOT / "bin" / "agentstack-codex-bootstrap", bootstrap)
    shutil.copy2(ROOT / "hooks" / "prepare-codex-session-binding.py", hooks)
    (libdir / "agentstack-register.sh").write_text(
        "ags_mail_load_token() { :; }\n"
        "ags_mcp_call() { printf '{\"result\":{}}\\n'; }\n"
        "ags_start_mail_watcher() { :; }\n"
        "ags_pick_adjective_scientist_name() { printf 'CandidateCodex\\n'; }\n"
        "ags_register_session() {\n"
        "  AGS_REGISTERED_AGENT_NAME=TopLevelCodex\n"
        "  AGS_REGISTERED_AGENT_ID=73\n"
        "  return 0\n"
        "}\n"
        "ags_record_managed_agent() { :; }\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    command = (
        f'source "{bootstrap}" "{project}" >/dev/null; '
        "result=$?; printf '%s|%s|%s\n' \"$result\" "
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
            "AGENTSTACK_PYTHON": sys.executable,
            "TMUX": "",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    status, launch_path, launch_id = result.stdout.strip().split("|")
    assert status == "0", result.stderr
    assert launch_id
    launch = json.loads(Path(launch_path).read_text(encoding="utf-8"))
    assert launch["launch_kind"] == "startup"
    assert launch["launch_origin"] == "standalone"
    assert "codex_mcp_profile" not in launch

    _seed_bound_receipt(
        runtime,
        project,
        tmp_path / "top-level-rollout.jsonl",
        agent_name="TopLevelCodex",
        launch_origin="standalone",
        launch_id=launch_id,
    )

    resume_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "clean-home"),
        "AGENTSTACK_PROJECT_KEY": str(project),
        "AGENTSTACK_RUNTIME_DIR": str(runtime),
        "AGENTSTACK_HOOKS_DIR": str(hooks),
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_RESERVED_IDENTITY": "1",
        "AGENTSTACK_CODEX_LAUNCH_KIND": "resume",
        "AGENTSTACK_CODEX_RESUME_SESSION_ID": SESSION_ID,
        "AGENTSTACK_CODEX_LAUNCH_ORIGIN": "standalone",
        "AGENT_NAME": "TopLevelCodex",
        "TMUX": "",
    }
    resumed = subprocess.run(
        ["bash", "-c", command],
        env=resume_env,
        text=True,
        capture_output=True,
        check=False,
    )
    status, resumed_path, resumed_id = resumed.stdout.strip().split("|")
    assert status == "0", resumed.stderr
    assert resumed_path == launch_path
    assert resumed_id and resumed_id != launch_id
    resume_launch = json.loads(Path(resumed_path).read_text(encoding="utf-8"))
    assert resume_launch["launch_kind"] == "resume"
    assert resume_launch["resume_session_id"] == SESSION_ID
    assert resume_launch["fallback_launch_id"] == launch_id
    assert resume_launch["launch_origin"] == "standalone"
    assert "codex_mcp_profile" not in resume_launch
    assert not (runtime / "child-agents" / "TopLevelCodex.json").exists()


@pytest.mark.parametrize(
    "failure_mode", ["session_missing", "project_unset", "health_unreachable"]
)
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
        "AGENTSTACK_CODEX_RESUME_SESSION_ID": (
            "" if failure_mode == "session_missing" else SESSION_ID
        ),
        "AGENTSTACK_CODEX_CHILD_MCP_PROFILE": "inherit",
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
    if failure_mode == "session_missing":
        assert "no valid target session id" in result.stderr
