from __future__ import annotations

import json
from pathlib import Path

from dashboard.providers.base import ActionResult, RuntimeProvider, RuntimeSnapshot
from dashboard.providers.tmux import TmuxRuntimeProvider


FIXTURE = Path(__file__).parent / "fixtures" / "tmux-runtimes.json"


def _fixture_state() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_tmux_provider_satisfies_runtime_contract_without_real_tmux():
    provider = TmuxRuntimeProvider(state_loader=_fixture_state, action_handlers={})

    assert isinstance(provider, RuntimeProvider)
    snapshots = provider.list_runtimes()
    assert all(isinstance(item, RuntimeSnapshot) for item in snapshots)
    assert [item.external_id for item in snapshots] == [
        "sample-agent",
        "waiting-agent",
    ]
    assert snapshots[0].state == "attached"
    assert snapshots[0].live == "Working on sample task"
    assert snapshots[1].state == "present"
    assert snapshots[1].live == ""
    assert snapshots[0].capabilities == frozenset({"open", "wake", "kill"})


def test_tmux_provider_delegates_actions_unchanged():
    calls: list[str] = []

    def open_runtime(external_id: str) -> dict:
        calls.append(external_id)
        return {"ok": True, "adapter": "test-terminal"}

    provider = TmuxRuntimeProvider(
        state_loader=lambda: {},
        action_handlers={"open": open_runtime},
    )
    result = provider.perform("sample-agent", "open")

    assert isinstance(result, ActionResult)
    assert result.ok is True
    assert result.details == {"ok": True, "adapter": "test-terminal"}
    assert calls == ["sample-agent"]


def test_tmux_provider_rejects_unadvertised_action():
    provider = TmuxRuntimeProvider(state_loader=lambda: {}, action_handlers={})
    result = provider.perform("sample-agent", "interrupt")
    assert result.ok is False
    assert result.error == "unsupported tmux action: interrupt"
