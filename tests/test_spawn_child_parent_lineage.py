"""A launcher-bound child's proxy knows who its parent is.

The proxy a launcher configures for a child is bound directly to the child's
identity. It used to report every such binding as `lineage.kind == "root"` with
no parent, because the launcher never told it one. A Codex child restarted
with `PARENT_AGENT=<parent>` compared that with its canonical task ("your
parent is <parent>"), saw a root, and stopped as an identity mismatch
(WSL2 report, 2026-10-01, problem 4).

Everything here runs against a temporary HOME/runtime and a disposable proxy
process whose Mail endpoint is a closed port: `bootstrap` and `runtime_status`
never reach Mail, so no service is involved.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tomllib

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_spawn_child_embed_task import (  # noqa: E402
    ROOT,
    SPAWN,
    _codex_handoff,
    _enable_fake_codex_profile,
    _executable,
    _fake_launch_env,
)


PROXY = ROOT / "integrations" / "codex_app" / "src" / "agentstack_codex_app" / "mcp_server.py"
PARENT = "ParentAgent"


def _spawn(env: dict, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", str(SPAWN), *args], cwd=ROOT, env=env, text=True,
                          capture_output=True, timeout=60, check=False)


def _launched(env: dict) -> bool:
    log = pathlib.Path(env["FAKE_TMUX_LOG"])
    return log.exists() and "new-session" in log.read_text(encoding="utf-8")


def _codex_child(tmp_path: pathlib.Path, name: str, *extra: str, profile: bool = True) -> tuple[dict, list[str]]:
    env, workdir = _fake_launch_env(tmp_path, codex=True)
    if profile:
        _enable_fake_codex_profile(tmp_path, env)
    handoff = _codex_handoff(tmp_path, name)
    task = tmp_path / "task.md"
    task.write_text("restarted task", encoding="utf-8")
    # --standalone takes its task positionally; --embed-task refuses it.
    source = ["standalone task"] if "--standalone" in extra else ["--embed-task", "--task-file", str(task)]
    return env, ["--pre-registered", name, "--child-token-file", str(handoff), "--codex", *extra,
                 *source, str(workdir)]


def _proxy_status(server: dict, tmp_path: pathlib.Path) -> dict:
    """Start the real proxy with the generated server's environment."""
    env = {**os.environ, **server["env"], "AGENTSTACK_MCP_URL": "http://127.0.0.1:9/mcp"}
    requests = [
        # What a Codex child actually sends: its own thread id.
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "bootstrap", "arguments": {"session_id": "019a-codex-thread"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "runtime_status", "arguments": {}}},
    ]
    proc = subprocess.run([sys.executable, str(PROXY)], env=env, text=True, capture_output=True, timeout=30,
                          input="".join(json.dumps(item) + "\n" for item in requests), check=False)
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert len(replies) == 2, proc.stderr
    bootstrap, status = (reply["result"]["structuredContent"] for reply in replies)
    assert bootstrap["lineage"] == status["lineage"]
    return status


def _codex_servers(env: dict, name: str) -> dict:
    home = pathlib.Path(env["AGENTSTACK_RUNTIME_DIR"]) / "child-agents" / f"{name}.codex-home"
    config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    servers = {key: value for key, value in config["mcp_servers"].items()
               if "AGENTSTACK_PROXY_AGENT_NAME" in (value.get("env") or {})}
    assert servers
    return servers


def test_a_restarted_codex_child_is_told_its_parent(tmp_path):
    name = "PeachFermi"
    env, args = _codex_child(tmp_path, name)
    env["PARENT_AGENT"] = PARENT

    result = _spawn(env, args)

    assert result.returncode == 0, result.stderr
    servers = _codex_servers(env, name)
    for server in servers.values():
        assert server["env"]["AGENTSTACK_PROXY_PARENT_AGENT"] == PARENT
    status = _proxy_status(servers["orrery-mail"], tmp_path)
    # Before: {"kind": "root", "parent_external_id": null} and the child stopped.
    assert status["lineage"]["kind"] == "child"
    assert status["lineage"]["parent_agent"] == PARENT
    assert status["agent_name"] == name


def test_a_standalone_codex_child_is_a_root_with_no_parent(tmp_path):
    name = "LoneFermi"
    env, args = _codex_child(tmp_path, name, "--standalone")

    result = _spawn(env, args)

    assert result.returncode == 0, result.stderr
    servers = _codex_servers(env, name)
    assert all("AGENTSTACK_PROXY_PARENT_AGENT" not in server["env"] for server in servers.values())
    status = _proxy_status(servers["orrery-mail"], tmp_path)
    assert status["lineage"]["kind"] == "root"
    assert status["lineage"]["parent_agent"] is None


def test_a_claude_child_proxy_is_told_its_parent_too(tmp_path):
    env, workdir = _fake_launch_env(tmp_path, codex=False)
    runner = tmp_path / "run-mcp.sh"
    _executable(runner, "#!/bin/bash\nexit 0\n")
    env["AGENTSTACK_MCP_PROXY"] = str(runner)
    env["PARENT_AGENT"] = PARENT
    name = "LuckyTesla"
    handoff = _codex_handoff(tmp_path, name)
    binding = handoff.with_name(handoff.name + ".binding.json")
    binding.write_text(json.dumps({**json.loads(binding.read_text()), "program": "claude-code"}))
    task = tmp_path / "task.md"
    task.write_text("claude task", encoding="utf-8")

    result = _spawn(env, ["--pre-registered", name, "--child-token-file", str(handoff),
                          "--embed-task", "--task-file", str(task), str(workdir)])

    assert result.returncode == 0, result.stderr
    config = json.loads((pathlib.Path(env["AGENTSTACK_RUNTIME_DIR"]) / "child-agents" / f"{name}.mcp.json").read_text())
    servers = config["mcpServers"]
    assert servers
    for server in servers.values():
        assert server["env"]["AGENTSTACK_PROXY_PARENT_AGENT"] == PARENT
    assert _proxy_status(servers["orrery-mail"], tmp_path)["lineage"]["parent_agent"] == PARENT


def test_a_parent_the_proxy_cannot_carry_stops_the_launch(tmp_path):
    """A name the proxy would refuse would leave the child with no Mail at all."""
    env, args = _codex_child(tmp_path, "OddParent")
    env["PARENT_AGENT"] = "Parent Agent/../x"

    result = _spawn(env, args)

    assert result.returncode != 0
    assert not _launched(env)
    assert "parent" in result.stderr.lower()


@pytest.mark.parametrize("enabled", [True, False], ids=["bridge-plugin", "no-plugin"])
def test_a_codex_child_without_its_proxy_does_not_inherit_the_bridge_lineage(tmp_path, enabled):
    """No generated home means the user's Codex config is used as-is.

    If that config enables the Codex App session-binding plugin, the child's
    `bootstrap` goes to the Bridge, which has never seen this child or its
    parent. That combination cannot report the parent, so it is refused before
    the CLI starts. Without the plugin there is no `bootstrap` at all and the
    existing shared-endpoint fallback stays as it was.
    """
    name = "NoProxy"
    env, args = _codex_child(tmp_path, name, profile=False)
    env["PARENT_AGENT"] = PARENT
    source = tmp_path / "source-codex-home"
    source.mkdir()
    (source / "config.toml").write_text(
        '[plugins."agentstack-codex-app@market"]\nenabled = true\n' if enabled else 'model = "gpt-5.5"\n',
        encoding="utf-8",
    )
    env["CODEX_HOME"] = str(source)

    result = _spawn(env, args)

    if enabled:
        assert result.returncode != 0
        assert not _launched(env)
        assert "parent" in result.stderr.lower()
    else:
        assert result.returncode == 0, result.stderr
        assert _launched(env)
