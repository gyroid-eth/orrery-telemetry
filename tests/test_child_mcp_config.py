#!/usr/bin/env python3
"""spawn_child.sh must give a Claude child its own authenticated MCP config.

Defect D, part 2 (tester report 2026-07-24 section 7): the child's MCP
connection was not authenticated as the child, so it could not read its own
inbox — the very place delegate puts its task. The launcher now points the
child's mcp-agent-mail server at the local stdio proxy, which holds the
child's owner token.

Runnable two ways (no third-party dependency required):
    python3 tests/test_child_mcp_config.py
    pytest tests/test_child_mcp_config.py
"""
from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SPAWN = _ROOT / "hooks" / "spawn_child.sh"
_CODEX_PROXY_TOOLS = (
    "bootstrap",
    "fetch_inbox",
    "send_message",
    "acknowledge_message",
    "reserve_files",
    "renew_reservations",
    "release_reservations",
    "runtime_status",
)


def _assert_proxy_tool_approvals(config: str, server_names: tuple[str, ...]) -> None:
    for server_name in server_names:
        for tool_name in _CODEX_PROXY_TOOLS:
            block = (
                f'[mcp_servers."{server_name}".tools."{tool_name}"]\n'
                'approval_mode = "approve"'
            )
            assert block in config, block
    assert config.count('approval_mode = "approve"') == (
        len(server_names) * len(_CODEX_PROXY_TOOLS)
    )
    assert "default_tools_approval_mode" not in config


def test_codex_child_approval_allowlist_matches_proxy_surface_exactly():
    source = _ROOT / "integrations" / "codex_app" / "src"
    sys.path.insert(0, str(source))
    try:
        from agentstack_codex_app.mcp_server import TOOL_DEFINITIONS
    finally:
        sys.path.pop(0)
    assert _CODEX_PROXY_TOOLS == tuple(item["name"] for item in TOOL_DEFINITIONS)


def _extract(func: str) -> str:
    text = _SPAWN.read_text(encoding="utf-8")
    start = text.index(f"{func}() {{")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _run_helper(tmpdir: pathlib.Path, *, runner_executable: bool = True,
                token: str | None = "child-owner-token") -> tuple[str, pathlib.Path]:
    runner = tmpdir / "run-mcp.sh"
    runner.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    if runner_executable:
        runner.chmod(runner.stat().st_mode | stat.S_IEXEC)

    token_file = tmpdir / "token"
    if token is not None:
        token_file.write_text(token, encoding="utf-8")

    script = (
        'RUNTIME_DIR="$1"; PROJECT_KEY="$2"; MCP_URL="$3"; MAIL_ENV="$4"; shift 4\n'
        + _extract("write_child_mcp_config")
        + '\nwrite_child_mcp_config "Red-Euler" "$1"\n'
    )
    env = os.environ.copy()
    env["AGENTSTACK_MCP_PROXY"] = str(runner)
    proc = subprocess.run(
        ["bash", "-c", script, "bash", str(tmpdir / "runtime"),
         "/workspace/example", "http://127.0.0.1:8765/mcp",
         str(tmpdir / "mail.env"), str(token_file)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False,
    )
    return proc.stdout.strip(), tmpdir


def test_config_points_the_child_at_the_authenticating_proxy():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        path, _ = _run_helper(tmpdir)
        assert path, "helper produced no config path"
        config = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        server = config["mcpServers"]["mcp-agent-mail"]
        assert server["command"].endswith("run-mcp.sh")
        env = server["env"]
        assert env["AGENTSTACK_PROXY_AGENT_NAME"] == "Red-Euler"
        assert env["AGENTSTACK_PROXY_TOKEN_FILE"].endswith("token")
        assert env["AGENTSTACK_PROJECT_KEY"] == "/workspace/example"
        # The token itself is never written into the config.
        assert "child-owner-token" not in json.dumps(config)


def test_config_file_is_not_world_readable():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        path, _ = _run_helper(tmpdir)
        mode = stat.S_IMODE(pathlib.Path(path).stat().st_mode)
        assert mode == 0o600, oct(mode)


def test_missing_proxy_or_token_falls_back_instead_of_failing_the_spawn():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        path, _ = _run_helper(tmpdir, runner_executable=False)
        assert path == "", "should print nothing when the proxy is unavailable"

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        path, _ = _run_helper(tmpdir, token=None)
        assert path == "", "should print nothing when the token file is missing"


def _run_codex_home(tmpdir: pathlib.Path, *, config_text: str | None = None,
                    runner_executable: bool = True,
                    token: str | None = "child-owner-token") -> str:
    runner = tmpdir / "run-mcp.sh"
    runner.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    if runner_executable:
        runner.chmod(runner.stat().st_mode | stat.S_IEXEC)

    token_file = tmpdir / "token"
    if token is not None:
        token_file.write_text(token, encoding="utf-8")

    source_home = tmpdir / "codex-home"
    source_home.mkdir()
    (source_home / "auth.json").write_text('{"token": "secret"}', encoding="utf-8")
    (source_home / "sessions").mkdir()
    if config_text is not None:
        (source_home / "config.toml").write_text(config_text, encoding="utf-8")

    script = (
        'RUNTIME_DIR="$1"; PROJECT_KEY="$2"; MCP_URL="$3"; MAIL_ENV="$4"; shift 4\n'
        + _extract("write_child_codex_home")
        + '\nwrite_child_codex_home "Red-Euler" "$1"\n'
    )
    env = os.environ.copy()
    env["AGENTSTACK_MCP_PROXY"] = str(runner)
    env["CODEX_HOME"] = str(source_home)
    proc = subprocess.run(
        ["bash", "-c", script, "bash", str(tmpdir / "runtime"),
         "/workspace/example", "http://127.0.0.1:8765/mcp",
         str(tmpdir / "mail.env"), str(token_file)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False,
    )
    return proc.stdout.strip()


_BASE_CODEX_CONFIG = """model = "gpt-5.5"

[mcp_servers.agent-mail]
url = "http://127.0.0.1:8765/api/"
bearer_token_env_var = "MCP_AGENT_MAIL_TOKEN"

[mcp_servers.agent-mail.tools.fetch_inbox]
approval_mode = "approve"

[mcp_servers.notion]
url = "https://mcp.notion.com/mcp"
enabled = false

[plugins."agentstack-codex-app@test-market"]
enabled = true

[plugins."agentstack-codex-app@test-market".mcp_servers.agentstack]
enabled = true
"""


def test_codex_child_gets_a_home_whose_agent_mail_is_the_proxy():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        home = _run_codex_home(tmpdir, config_text=_BASE_CODEX_CONFIG)
        assert home, "helper produced no CODEX_HOME"
        config = (pathlib.Path(home) / "config.toml").read_text(encoding="utf-8")

        # The shared HTTP transport for agent-mail is gone, replaced by stdio.
        assert 'url = "http://127.0.0.1:8765/api/"' not in config
        assert '[mcp_servers."agent-mail"]' in config
        assert '[mcp_servers."agentstack"]' in config
        assert "command = " in config
        assert 'AGENTSTACK_PROXY_AGENT_NAME = "Red-Euler"' in config
        assert (
            'AGENTSTACK_CODEX_APP_RUNTIME_DIR = "'
            in config
        )
        assert "/Red-Euler.codex-home/proxy-runtime\"" in config
        # The source transport's approval table is replaced by the complete,
        # fixed proxy allowlist. Shell approvals and unrelated MCP servers stay
        # untouched.
        _assert_proxy_tool_approvals(config, ("agent-mail", "agentstack"))
        assert (
            '[plugins."agentstack-codex-app@test-market".'
            'mcp_servers.agentstack]\nenabled = false'
        ) in config
        # Unrelated config survives untouched.
        assert 'model = "gpt-5.5"' in config
        assert "[mcp_servers.notion]" in config
        # The token itself is never written into the config.
        assert "child-owner-token" not in config


def test_codex_child_home_shares_login_and_history_but_owns_its_config():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        home = pathlib.Path(_run_codex_home(tmpdir, config_text=_BASE_CODEX_CONFIG))
        assert (home / "auth.json").is_symlink(), "child must reuse the real login"
        assert (home / "sessions").is_symlink()
        assert not (home / "config.toml").is_symlink(), "config must be child-owned"
        assert stat.S_IMODE((home / "config.toml").stat().st_mode) == 0o600


def test_codex_child_home_works_when_the_user_has_no_config():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        home = _run_codex_home(tmpdir, config_text=None)
        config = (pathlib.Path(home) / "config.toml").read_text(encoding="utf-8")
        assert '[mcp_servers."agent-mail"]' in config
        assert '[mcp_servers."agentstack"]' in config
        _assert_proxy_tool_approvals(config, ("agent-mail", "agentstack"))


def test_codex_child_home_falls_back_when_proxy_or_token_is_missing():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        assert _run_codex_home(tmpdir, config_text=_BASE_CODEX_CONFIG,
                               runner_executable=False) == ""
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        assert _run_codex_home(tmpdir, config_text=_BASE_CODEX_CONFIG, token=None) == ""


def test_both_codex_launch_paths_use_the_child_home():
    text = _SPAWN.read_text(encoding="utf-8")
    assert text.count('CHILD_CODEX_HOME="$(write_child_codex_home') == 2
    assert text.count('-e "CODEX_HOME=$CHILD_CODEX_HOME"') == 2
    # The user's optional workspace launcher intentionally reads this override;
    # without it, that wrapper replaces the child home with ~/.codex again.
    assert text.count('-e "CODEX_SHARED_CODEX_DIR=$CHILD_CODEX_HOME"') == 2


def test_launcher_passes_the_config_to_claude_only_when_present():
    text = _SPAWN.read_text(encoding="utf-8")
    assert 'CHILD_MCP_CONFIG="$(write_child_mcp_config' in text
    assert '-e "CLAUDE_CHILD_MCP_CONFIG=$CHILD_MCP_CONFIG"' in text
    # Empty config must not turn into a bare `--mcp-config` with no value.
    assert (
        'MCP_ARGS=(--mcp-config "$CLAUDE_CHILD_MCP_CONFIG" --strict-mcp-config)'
        in text
    )
    assert '[[ -n "$CLAUDE_CHILD_MCP_CONFIG" ]]' in text
    # --strict-mcp-config keeps the child on its own proxy. Without it the child
    # also inherits the user's top-level mcpServers, ends up talking to a second
    # copy of agent-mail, and carries a standing authentication notice.
    assert text.count("--strict-mcp-config") == 2


def _extract_install_fn(func: str) -> str:
    text = (_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    start = text.index(f"{func}() {{")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def test_installer_ships_the_child_mcp_proxy():
    """The launcher's proxy path must exist after a normal install.

    Found via the tester's 2026-07-31 re-test: children were falling back to
    the shared endpoint because integrations/ was never installed.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        install_dir = tmpdir / "agentstack"
        script = (
            'REPO_ROOT="$1"; INSTALL_DIR="$2"; DRY_RUN=false\n'
            'plan() { echo "$*"; }\n'
            'warn() { echo "warning: $*" >&2; }\n'
            + _extract_install_fn("install_child_mcp_proxy")
            + "\ninstall_child_mcp_proxy\n"
        )
        result = subprocess.run(
            ["bash", "-c", script, "bash", str(_ROOT), str(install_dir)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        assert result.returncode == 0, result.stderr

        # Exactly the path hooks/spawn_child.sh defaults to.
        runner = install_dir / "integrations" / "codex_app" / "plugin" / "scripts" / "run-mcp.sh"
        assert runner.is_file(), "proxy runner not installed"
        assert os.access(runner, os.X_OK), "proxy runner is not executable"

        # ...and the package it execs, or the runner dies on first use.
        package = install_dir / "integrations" / "codex_app" / "src" / "agentstack_codex_app"
        assert (package / "mcp_server.py").is_file()
        for module in ("agent_mail_client.py", "hook_entry.py", "identity_store.py",
                       "snapshot.py"):
            assert (package / module).is_file(), module

        # Build artefacts must not ship.
        assert not list(package.rglob("__pycache__")), "shipped __pycache__"
        assert not list(package.rglob("*.pyc")), "shipped .pyc files"


def test_installed_proxy_path_matches_what_the_launcher_looks_for():
    """A path drift between installer and launcher reintroduces the fallback."""
    spawn = _SPAWN.read_text(encoding="utf-8")
    assert "/integrations/codex_app/plugin/scripts/run-mcp.sh" in spawn
    install = (_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "integrations/codex_app" in install
    assert "install_child_mcp_proxy" in install


def test_machine_wide_env_file_cannot_override_the_child_identity():
    """The runner sources the bridge's env.sh; the caller must still win.

    That file uses plain `export`, so before this guard a machine that had the
    Codex App bridge installed would rebind every spawned child to the bridge's
    project_key and endpoint instead of its own.
    """
    runner = _ROOT / "integrations" / "codex_app" / "plugin" / "scripts" / "run-mcp.sh"
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)

        # A bridge env.sh that disagrees with the caller on every shared key.
        install_dir = tmpdir / "codex_app"
        install_dir.mkdir()
        (install_dir / "env.sh").write_text(
            "export AGENTSTACK_PROJECT_KEY=/bridge/project\n"
            "export AGENTSTACK_MCP_URL=http://127.0.0.1:8765/api/\n",
            encoding="utf-8",
        )

        # Stand in for python3 so no server is needed: dump what the proxy would see.
        stub = tmpdir / "fake-python"
        stub.write_text(
            "#!/bin/bash\n"
            'printf "%s\\n" "$AGENTSTACK_PROJECT_KEY" "$AGENTSTACK_MCP_URL" '
            '"$AGENTSTACK_PROXY_AGENT_NAME"\n',
            encoding="utf-8",
        )
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

        env = os.environ.copy()
        env.update({
            "AGENTSTACK_CODEX_APP_INSTALL_DIR": str(install_dir),
            "AGENTSTACK_PYTHON": str(stub),
            "AGENTSTACK_PROJECT_KEY": "/child/project",
            "AGENTSTACK_MCP_URL": "http://127.0.0.1:19999/mcp",
            "AGENTSTACK_PROXY_AGENT_NAME": "Dark-Langmuir",
        })
        result = subprocess.run(
            ["/bin/bash", str(runner)], text=True, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        assert result.returncode == 0, result.stderr
        project, url, agent = result.stdout.strip().splitlines()[:3]
        assert project == "/child/project", f"bridge env.sh overrode project_key: {project}"
        assert url == "http://127.0.0.1:19999/mcp", f"bridge env.sh overrode endpoint: {url}"
        assert agent == "Dark-Langmuir"


def test_env_file_still_supplies_values_the_caller_omitted():
    """Caller-wins must not turn into caller-only: unset keys still come from env.sh."""
    runner = _ROOT / "integrations" / "codex_app" / "plugin" / "scripts" / "run-mcp.sh"
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        install_dir = tmpdir / "codex_app"
        install_dir.mkdir()
        (install_dir / "env.sh").write_text(
            "export AGENTSTACK_MCP_URL=http://127.0.0.1:8765/api/\n", encoding="utf-8")
        stub = tmpdir / "fake-python"
        stub.write_text('#!/bin/bash\nprintf "%s\\n" "$AGENTSTACK_MCP_URL"\n', encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

        env = os.environ.copy()
        env.update({
            "AGENTSTACK_CODEX_APP_INSTALL_DIR": str(install_dir),
            "AGENTSTACK_PYTHON": str(stub),
        })
        env.pop("AGENTSTACK_MCP_URL", None)
        result = subprocess.run(
            ["/bin/bash", str(runner)], text=True, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "http://127.0.0.1:8765/api/"


_TESTER_CLAUDE_JSON = """{
  "mcpServers": {
    "mcp_agent_mail": {"type": "http", "url": "http://127.0.0.1:8765/api/"},
    "semantic-search": {"command": "semantic"}
  },
  "projects": {"/p": {"mcpServers": {"freecad": {"command": "fc"}}}}
}
"""


def _claude_child_config(tmpdir, claude_json: str | None) -> dict:
    runner = tmpdir / "run-mcp.sh"
    runner.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    runner.chmod(runner.stat().st_mode | stat.S_IEXEC)
    token_file = tmpdir / "token"
    token_file.write_text("child-owner-token", encoding="utf-8")
    settings = tmpdir / "claude.json"
    if claude_json is not None:
        settings.write_text(claude_json, encoding="utf-8")

    script = (
        'RUNTIME_DIR="$1"; PROJECT_KEY="$2"; MCP_URL="$3"; MAIL_ENV="$4"; shift 4\n'
        + _extract("write_child_mcp_config")
        + '\nwrite_child_mcp_config "Dark-Feynman" "$1"\n'
    )
    env = os.environ.copy()
    env["AGENTSTACK_MCP_PROXY"] = str(runner)
    env["AGENTSTACK_CLAUDE_JSON"] = str(settings)
    proc = subprocess.run(
        ["bash", "-c", script, "bash", str(tmpdir / "runtime"), "/p",
         "http://127.0.0.1:8765/mcp", str(tmpdir / "mail.env"), str(token_file)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False,
    )
    assert proc.stdout.strip(), proc.stderr
    return json.loads(pathlib.Path(proc.stdout.strip()).read_text(encoding="utf-8"))


def test_claude_child_proxy_claims_the_users_own_server_name():
    """Otherwise the child sees two agent-mail servers and picks the direct one.

    Measured: --mcp-config overrides a same-named global server, so claiming
    the user's name replaces their unauthenticated HTTP connection.
    """
    with tempfile.TemporaryDirectory() as tmp:
        config = _claude_child_config(pathlib.Path(tmp), _TESTER_CLAUDE_JSON)
        servers = config["mcpServers"]
        assert "mcp_agent_mail" in servers, servers
        assert servers["mcp_agent_mail"]["command"].endswith("run-mcp.sh")
        # Only agent-mail is claimed; the user's other servers are untouched.
        assert "semantic-search" not in servers
        assert "freecad" not in servers


def test_claude_child_falls_back_to_a_default_name_without_user_config():
    with tempfile.TemporaryDirectory() as tmp:
        config = _claude_child_config(pathlib.Path(tmp), None)
        assert list(config["mcpServers"]) == ["mcp-agent-mail"]


def test_codex_child_replaces_every_agent_mail_spelling():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        source = (
            'model = "gpt-5.6-sol"\n\n'
            "[mcp_servers.mcp_agent_mail]\n"
            'url = "http://127.0.0.1:8765/mcp"\n\n'
            "[mcp_servers.mcp_agent_mail.tools.fetch_inbox]\n"
            'approval_mode = "approve"\n\n'
            "[mcp_servers.agent-mail]\n"
            'url = "http://127.0.0.1:8765/api/"\n\n'
            "[mcp_servers.notion]\n"
            'url = "https://mcp.notion.com/mcp"\n'
        )
        home = pathlib.Path(_run_codex_home(tmpdir, config_text=source))
        text = (home / "config.toml").read_text(encoding="utf-8")

        # Every direct agent-mail transport is gone (the endpoint still appears
        # inside the proxy's env block, which is what the proxy dials).
        assert 'url = "http://127.0.0.1:8765/mcp"' not in text
        assert 'url = "http://127.0.0.1:8765/api/"' not in text
        assert "tools.fetch_inbox" not in text
        # ...and both names now resolve to the proxy.
        assert '[mcp_servers."mcp_agent_mail"]' in text
        assert '[mcp_servers."agent-mail"]' in text
        assert '[mcp_servers."agentstack"]' in text
        assert text.count("run-mcp.sh") >= 3
        _assert_proxy_tool_approvals(
            text, ("mcp_agent_mail", "agent-mail", "agentstack")
        )
        # Unrelated config survives.
        assert "[mcp_servers.notion]" in text
        assert 'model = "gpt-5.6-sol"' in text


def test_doctor_reports_the_fallback_instead_of_staying_silent():
    doctor = (_ROOT / "scripts" / "doctor.sh").read_text(encoding="utf-8")
    assert "child MCP proxy" in doctor
    assert "fall back to the shared agent-mail endpoint" in doctor


def _main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("\n" + ("ALL PASSED" if not failures else f"{failures} FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
