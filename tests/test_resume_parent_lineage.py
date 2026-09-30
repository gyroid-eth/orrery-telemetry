"""A resumed child's Mail proxy still knows who its parent is.

`spawn_child.sh` now hands the child's proxy its parent so bootstrap and
runtime_status report `lineage.parent_agent` (WSL2 report, problem 4). A child
resumed from the dashboard got a freshly written proxy config with no parent,
so it was back to claiming to be a root. The launcher records the parent in
the child's private state; every rebuild of the proxy config reads it there.

Temporary runtime and HOME only; Mail is a stand-in.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from dashboard import server
from hooks import child_resume
from test_claude_resume_mail import NAME, TOKEN, private, resume  # noqa: F401 - fixture


PARENT = "ParentAgent"


def _child(runtime: Path, registration: dict, *, parent: str | None) -> Path:
    state = runtime / "child-agents" / f"{NAME}.json"
    private(state, json.dumps({**registration, "registration_token": TOKEN}))
    child_resume.prepare_active_state(runtime, NAME, project_key=registration["project_key"],
                                      parent_agent=parent)
    assert child_resume.mark_retired(runtime, NAME, retention_days=30)
    return state


def test_the_parent_survives_exit_and_reaches_the_resumed_proxy(resume):  # noqa: F811
    runtime, registration, launches, _calls = resume
    state = _child(runtime, registration, parent=PARENT)
    assert json.loads(state.read_text())["parent_agent"] == PARENT

    result = server.do_resume(NAME)

    assert result["ok"], result
    config = json.loads((runtime / "child-agents" / f"{NAME}.mcp.json").read_text())
    assert config["mcpServers"]
    for proxy in config["mcpServers"].values():
        assert proxy["env"]["AGENTSTACK_PROXY_PARENT_AGENT"] == PARENT


def test_a_child_with_no_recorded_parent_is_resumed_without_one(resume):  # noqa: F811
    runtime, registration, _launches, _calls = resume
    _child(runtime, registration, parent=None)

    result = server.do_resume(NAME)

    assert result["ok"], result
    config = json.loads((runtime / "child-agents" / f"{NAME}.mcp.json").read_text())
    assert all("AGENTSTACK_PROXY_PARENT_AGENT" not in proxy["env"] for proxy in config["mcpServers"].values())


def test_a_parent_the_proxy_would_refuse_stops_the_resume(resume):  # noqa: F811
    """The proxy exits at startup on such a name: the child would have no Mail."""
    runtime, registration, launches, _calls = resume
    state = _child(runtime, registration, parent=PARENT)
    data = json.loads(state.read_text())
    data["parent_agent"] = "Parent Agent/../x"
    private(state, json.dumps(data))

    result = server.do_resume(NAME)

    assert not result["ok"]
    assert not launches


def test_recording_refuses_a_parent_the_proxy_cannot_carry(tmp_path):
    runtime = tmp_path / "runtime"
    private(runtime / f"agent_token_{NAME}", TOKEN)
    private(runtime / "child-agents" / f"{NAME}.json", json.dumps({
        "agent_id": 73, "agent_name": NAME, "project_key": "/p", "program": "claude-code",
        "registration_token": TOKEN,
    }))
    with pytest.raises(child_resume.ResumeStateError):
        child_resume.prepare_active_state(runtime, NAME, project_key="/p", parent_agent="Parent Agent")
    # An empty parent is a standalone relaunch: it clears an earlier one.
    child_resume.prepare_active_state(runtime, NAME, project_key="/p", parent_agent=PARENT)
    child_resume.prepare_active_state(runtime, NAME, project_key="/p", parent_agent="")
    assert "parent_agent" not in json.loads((runtime / "child-agents" / f"{NAME}.json").read_text())


@pytest.mark.parametrize("parent", [PARENT, None], ids=["child", "standalone"])
def test_a_resumed_codex_home_names_the_recorded_parent(tmp_path, parent):
    """The dashboard's Codex resume rebuilds the home without naming a parent."""
    runtime = tmp_path / "runtime"
    private(runtime / f"agent_token_{NAME}", TOKEN)
    private(runtime / "child-agents" / f"{NAME}.json", json.dumps({
        "agent_id": 73, "agent_name": NAME, "project_key": "/p", "program": "codex",
        "registration_token": TOKEN,
    }))
    child_resume.prepare_active_state(runtime, NAME, project_key="/p", program="codex", parent_agent=parent)
    source = tmp_path / "codex-home"
    source.mkdir()
    runner = tmp_path / "run-mcp.sh"
    runner.write_text("#!/bin/bash\nexit 0\n")
    runner.chmod(0o755)

    home = child_resume.build_home(
        home=runtime / "child-agents" / f"{NAME}.codex-home", source=source, runner=runner, child=NAME,
        project_key="/p", token_file=runtime / f"agent_token_{NAME}", mcp_url="http://127.0.0.1:9/mcp",
        mail_env="", runtime_dir=runtime, bearer_mode="disabled", python_bin="", mcp_profile="inherit",
    )

    servers = tomllib.loads((home / "config.toml").read_text())["mcp_servers"]
    proxies = [server["env"] for server in servers.values() if "AGENTSTACK_PROXY_AGENT_NAME" in server.get("env", {})]
    assert proxies
    for env in proxies:
        assert env.get("AGENTSTACK_PROXY_PARENT_AGENT") == parent
