from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess


ROOT = pathlib.Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "bin" / "agentstack-gemini-mcp"


def _write_proxy(path: pathlib.Path) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        "print(json.dumps({\n"
        "  'agent': os.environ.get('AGENTSTACK_PROXY_AGENT_NAME'),\n"
        "  'token_file': os.environ.get('AGENTSTACK_PROXY_TOKEN_FILE'),\n"
        "  'program': os.environ.get('AGENTSTACK_PROXY_PROGRAM'),\n"
        "  'project_key': os.environ.get('AGENTSTACK_PROJECT_KEY'),\n"
        "  'runtime_dir': os.environ.get('AGENTSTACK_RUNTIME_DIR'),\n"
        "}))\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_session_bound_wrapper_resolves_owner_token_path_at_process_start(tmp_path):
    runtime = tmp_path / "runtime dir"
    runtime.mkdir()
    agent = "CosmicEinstein"
    token_file = runtime / f"agent_token_{agent}"
    token_file.write_text("owner-secret", encoding="utf-8")
    token_file.chmod(0o600)

    proxy = tmp_path / "fake proxy.py"
    _write_proxy(proxy)

    env = {
        **os.environ,
        "AGENT_NAME": agent,
        "AGENTSTACK_PROJECT_KEY": str(tmp_path / "project"),
        "AGENTSTACK_RUNTIME_DIR": str(runtime),
        "AGENTSTACK_MCP_PROXY": str(proxy),
        "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled",
    }
    result = subprocess.run(
        ["bash", str(WRAPPER)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "agent": agent,
        "token_file": str(token_file),
        "program": "antigravity",
        "project_key": str(tmp_path / "project"),
        "runtime_dir": str(runtime),
    }
    # The wrapper passes only the path to the narrow proxy. The token value is
    # not copied into argv or another environment variable by this layer.
    assert "owner-secret" not in result.stdout
    assert "owner-secret" not in result.stderr


def test_session_bound_wrapper_fails_closed_when_owner_token_is_missing(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    proxy = tmp_path / "proxy.py"
    _write_proxy(proxy)

    result = subprocess.run(
        ["bash", str(WRAPPER)],
        cwd=ROOT,
        env={
            **os.environ,
            "AGENT_NAME": "MissingTokenAgent",
            "AGENTSTACK_PROJECT_KEY": str(tmp_path / "project"),
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_MCP_PROXY": str(proxy),
            "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "no owner-token file" in result.stderr
    assert result.stdout == ""
