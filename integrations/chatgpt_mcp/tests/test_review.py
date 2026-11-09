"""Regressions for upstream #399's read-only boundary review."""
from copy import deepcopy

import pytest

from orrery_chatgpt_mcp.model import DashboardError, ToolError, invoke, snapshot
from orrery_chatgpt_mcp.server import Config, ConfigurationError

PROJECT = "/fixture/selected"
VERSION = {"name": "orrery-telemetry", "api": 38, "version": "test"}
ROSTER = {
    "ts": 100, "history_days": 30.0, "project_key": PROJECT,
    "agents": [
        {"name": name, "category": category, "project_key": PROJECT,
         "running": category == "agent", "model_raw": "fixture-model"}
        for name, category in (("LiveAgent", "agent"), ("GoneAgent", "gone"),
                               ("RetiredAgent", "retired"), ("FinishedAgent", "finished"))
    ] + [
        {"name": "ForeignAgent", "category": "agent", "project_key": "/fixture/other",
         "running": True, "model_raw": "foreign-private-model", "last_active": 555},
        {"name": "UnknownProject", "category": "agent", "project_key": None, "running": True},
        {"name": "Warmup", "category": "warmup", "project_key": PROJECT, "running": True},
    ],
}


class Backend:
    def __init__(self, version=None, roster=None):
        self.version = deepcopy(VERSION if version is None else version)
        self.roster = deepcopy(ROSTER if roster is None else roster)
        self.calls = []

    def read(self, path):
        self.calls.append(path)
        return self.version if path == "/api/version" else self.roster


def test_scoped_legacy_list_status_and_get_keep_stopped_agents():
    backend = Backend()
    result = invoke("orrery_list_agents", {}, backend, "legacy", project_key=PROJECT)
    assert [r["name"] for r in result["agents"]] == ["LiveAgent", "GoneAgent", "RetiredAgent", "FinishedAgent"]
    assert result["scope"]["project_key"] == PROJECT
    assert result["scope"]["history_days"] == 30
    assert "/api/agents?days=30" in backend.calls
    assert "foreign-private-model" not in str(result)
    assert "ForeignAgent" not in str(result)
    for name in ("GoneAgent", "RetiredAgent", "FinishedAgent"):
        result = invoke("orrery_get_agent", {"name": name}, backend, "legacy", project_key=PROJECT)
        assert result["agent"]["name"] == name
        assert result["scope"]["history_days"] == 30
    status = invoke("orrery_status", {}, backend, "legacy", project_key=PROJECT)
    assert status["observations"]["registered"] == 4
    assert status["observations"]["running"] == 1
    for name in ("ForeignAgent", "UnknownProject", "Warmup"):
        with pytest.raises(ToolError, match="unique"):
            invoke("orrery_get_agent", {"name": name}, backend, "legacy", project_key=PROJECT)


@pytest.mark.parametrize("api", [999, 39, 0, -1, True, "38", None])
def test_unknown_generations_fail_before_reading_any_roster(api):
    backend = Backend({**VERSION, "api": api})
    with pytest.raises(DashboardError):
        snapshot(backend, "legacy", project_key=PROJECT)
    assert backend.calls == ["/api/version"]


@pytest.mark.parametrize("api", [37, 38])
def test_supported_legacy_generations_preserve_project_scope(api):
    data = snapshot(Backend({**VERSION, "api": api}), "legacy", project_key=PROJECT)
    assert data.api == api
    assert data.scope["project_key"] == PROJECT
    assert {row["name"] for row in data.agents} == {"LiveAgent", "GoneAgent", "RetiredAgent", "FinishedAgent"}


@pytest.mark.parametrize("api", [34, 35, 36])
def test_old_legacy_contract_cannot_prove_project_scope(api):
    with pytest.raises(DashboardError):
        snapshot(Backend({**VERSION, "api": api}), "legacy", project_key=PROJECT)


@pytest.mark.parametrize("change", [{"project_key": "/fixture/other"}, {"project_key": None},
                                   {"history_days": 7}, {"history_days": None}])
def test_wrong_roster_scope_fails_closed(change):
    with pytest.raises(DashboardError):
        snapshot(Backend(roster={**ROSTER, **change}), "legacy", project_key=PROJECT)


@pytest.mark.parametrize("name", ["A" * 65, "A" * 64 + "/escape", "A" * 64 + "\n", 123, None])
def test_whole_agent_name_is_validated_without_truncation(name):
    roster = deepcopy(ROSTER)
    roster["agents"][0]["name"] = name
    with pytest.raises(DashboardError):
        snapshot(Backend(roster=roster), "legacy", project_key=PROJECT)


def test_config_requires_local_project_and_fresh_does_not_silently_ignore_it():
    with pytest.raises(ConfigurationError):
        Config.from_env({"ORRERY_MCP_MODE": "legacy"})
    with pytest.raises(ConfigurationError):
        Config.from_env({"ORRERY_MCP_MODE": "fresh", "ORRERY_MCP_PROJECT_KEY": PROJECT})
    config = Config.from_env({"ORRERY_MCP_MODE": "legacy", "ORRERY_MCP_PROJECT_KEY": PROJECT})
    assert config.project_key == PROJECT


@pytest.mark.parametrize("key", ["", " ", "line\nbreak", "x" * 4097])
def test_invalid_project_configuration_is_rejected(key):
    with pytest.raises(ConfigurationError):
        Config.from_env({"ORRERY_MCP_MODE": "legacy", "ORRERY_MCP_PROJECT_KEY": key})


@pytest.mark.parametrize("extra", [{"runtime_mode": None}, {"runtime_mode": ""},
                                  {"mail_instance_id": "a" * 32}, {"capabilities": {}}])
def test_legacy_does_not_accept_ambiguous_mode_fields(extra):
    backend = Backend({**VERSION, **extra})
    with pytest.raises(DashboardError):
        snapshot(backend, "legacy", project_key=PROJECT)
    assert backend.calls == ["/api/version"]


@pytest.mark.parametrize("tool", ["orrery_status", "orrery_list_agents", "orrery_get_agent"])
def test_remote_tool_arguments_cannot_change_local_project_scope(tool):
    backend = Backend()
    with pytest.raises(ToolError) as error:
        invoke(tool, {"project_key": "/fixture/other"}, backend, "legacy", project_key=PROJECT)
    assert error.value.code == "invalid_arguments"
    assert backend.calls == []
