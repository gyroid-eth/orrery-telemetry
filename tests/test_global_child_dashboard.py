"""The existing jump endpoint composes the canonical child resume entry."""

import json
import subprocess
import urllib.request

import pytest

import dashboard.server as dashboard
from test_dashboard_quota_http import _serve_once
from test_global_child_client import client, server as fixture_server

server = fixture_server


def test_global_jump_prepares_same_identity_without_legacy_lookup(server, monkeypatch):
    parent = client(server)
    prepared = parent.prepare_child(
        {"child_client_name": "child_01", "name": "ChildAlpha", "prepare_only": True}
    )
    monkeypatch.setattr(
        dashboard, "do_jump", lambda *a, **k: pytest.fail("legacy lookup")
    )
    http, worker = _serve_once()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{http.server_port}/api/jump",
            data=json.dumps(
                {
                    "runtime_mode": "global",
                    "client_config": str(parent.path),
                    "client_name": "child_01",
                    "prepare_only": True,
                    "open": False,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    finally:
        worker.join(timeout=35)
        http.server_close()
    assert (
        result["agent_id"] == prepared["agent_id"] and result["runtime_ready"] is False
    )


@pytest.mark.parametrize(
    "values", [{}, {"open": True}, {"open": 0}, {"prepare_only": False}]
)
def test_global_jump_refuses_actual_execution_before_process(values, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("side effect"))
    result = dashboard.global_child_resume(
        {"client_name": "child_01", "client_config": "/fixture/context", **values}
    )
    assert result["error"] == "GLOBAL_CHILD_RUNTIME_REQUIRES_PR4C"
