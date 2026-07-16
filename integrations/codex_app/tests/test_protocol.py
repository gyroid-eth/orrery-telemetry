from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentstack_codex_app.protocol import AppServerClient, AppServerError


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
FAKE_SERVER = PACKAGE_ROOT / "fixtures" / "fake_app_server.py"


@pytest.fixture
def client():
    value = AppServerClient((sys.executable, str(FAKE_SERVER)), timeout=2)
    try:
        yield value
    finally:
        value.close()


def test_initialize_then_thread_list_uses_snake_case_enum(client: AppServerClient):
    result = client.thread_list(limit=5, sort_key="updated_at")

    assert result["initialized"] is True
    assert result["method"] == "thread/list"
    assert result["params"] == {
        "limit": 5,
        "sortKey": "updated_at",
        "sortDirection": "desc",
        "useStateDbOnly": False,
    }


def test_thread_and_turn_methods_encode_protocol_params(client: AppServerClient):
    thread = client.thread_start(
        cwd="/workspace/example-project",
        model="gpt-example",
        sandbox="workspace-write",
        approval_policy="never",
    )
    assert thread["method"] == "thread/start"
    assert thread["thread"]["id"] == "thread-example"
    assert client.notifications[-1]["method"] == "thread/started"

    turn = client.turn_start("thread-example", "Inspect the sample fixture.")
    assert turn["method"] == "turn/start"
    assert turn["params"] == {
        "threadId": "thread-example",
        "input": [{"type": "text", "text": "Inspect the sample fixture."}],
    }


def test_thread_inject_items_uses_current_snake_case_method(client: AppServerClient):
    items = [{"type": "message", "role": "user", "content": []}]
    result = client.thread_inject_items("thread-example", items)
    assert result == {
        "method": "thread/inject_items",
        "params": {"threadId": "thread-example", "items": items},
    }


def test_json_rpc_errors_are_raised_with_method_context(client: AppServerClient):
    with pytest.raises(AppServerError, match="test/error failed: expected fake failure"):
        client.request("test/error")


@pytest.mark.parametrize("value", ["updatedAt", "UPDATED_AT", "unknown"])
def test_thread_list_rejects_non_protocol_sort_enums(
    client: AppServerClient, value: str
):
    with pytest.raises(ValueError, match="sort key"):
        client.thread_list(sort_key=value)
