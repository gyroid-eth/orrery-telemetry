"""API 36: one agent's history binding without the roster.

`GET /api/agents/<name>/history-binding` must give exactly the verdict of that
agent's `/api/agents?scope=live` row, without pane captures.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from dashboard import server
from test_agent_lookup import NAMES, world  # noqa: F401  (fixture)


def _fake_bindings(monkeypatch):
    """Deterministic per-name binding answers, recording each call's inputs."""
    seen = []

    def make(kind):
        def binding(name, **kwargs):
            seen.append((kind, name, kwargs.get("session_created")))
            return {"history_binding": "bound", "transcript_path": f"/fixture/{kind}/{name}.jsonl",
                    "history_binding_reason": "", "history_binding_reason_code": "bound",
                    "history_session_id": f"sid-{name}", "history_receipt_ts": "2026-11-04T00:00:00Z"}
        return binding
    monkeypatch.setattr(server, "_codex_history_binding", make("codex"))
    monkeypatch.setattr(server, "_claude_history_binding", make("claude"))
    monkeypatch.setattr(server, "_dsh_history_binding", make("dsh"))
    monkeypatch.setattr(server, "_hermes_history_binding", make("hermes"))
    return seen


@pytest.mark.parametrize("days", [30.0, 36500.0])
@pytest.mark.parametrize("name", NAMES + ["NoSuchAgent"])
def test_route_gives_the_live_roster_rows_binding(world, monkeypatch, name, days):  # noqa: F811
    # Both windows: the fixture's fixed dates fall out of 30 days as time
    # passes, so the long window keeps every agent in the comparison.
    _fake_bindings(monkeypatch)
    monkeypatch.setattr(server, "HISTORY_DAYS_DEFAULT", days)
    live = {row["name"]: row for row in server.build_agents(days)
            if server._is_live_roster_row(row)}
    if days > 1000:
        assert {"LiveClaude", "AppAgent"} <= set(live)
    found = server.history_binding_row(name)
    if name not in live:
        assert found is None
        return
    assert list(found) == list(server.HISTORY_BINDING_FIELDS)
    for key in server.HISTORY_BINDING_FIELDS:
        assert found[key] == live[name].get(key), (name, key)


def test_route_reads_no_pane_and_no_other_row(world, monkeypatch):  # noqa: F811
    seen = _fake_bindings(monkeypatch)

    def forbidden(*_a, **_k):
        raise AssertionError("the binding route must not capture a pane")
    monkeypatch.setattr(server, "_agent_runtime", forbidden)
    monkeypatch.setattr(server, "_native_context_runtime", forbidden)
    found = server.history_binding_row("LiveClaude")
    assert found["history_binding"] == "bound" and found["created"] == 100
    assert seen == [("claude", "LiveClaude", 100)]


@pytest.fixture
def http():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def get(path, method="GET", origin=None, body=b"{}"):
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        if origin:
            headers["Origin"] = origin
        request = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}{path}", method=method,
                                         data=body if method == "POST" else None, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
    get.base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        yield get
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_shape_and_refusals(world, monkeypatch, http):  # noqa: F811
    _fake_bindings(monkeypatch)
    status, body = http("/api/agents/LiveClaude/history-binding")
    payload = json.loads(body)
    assert status == 200 and payload["ok"] is True
    assert set(payload["agent"]) == set(server.HISTORY_BINDING_FIELDS)
    assert payload["agent"]["transcript_path"] == "/fixture/claude/LiveClaude.jsonl"
    for path in ("/api/agents/GoneClaude/history-binding", "/api/agents/Bad%2Fname/history-binding",
                 "/api/agents/../history-binding", "/api/agents//history-binding"):
        status, body = http(path)
        assert status == 404 and json.loads(body) == {"ok": False, "error": "agent_unavailable"}, path


@pytest.mark.parametrize("mode", ["FRESH_MODE", "GLOBAL_MODE"])
def test_fresh_and_global_answer_the_route_as_missing(world, monkeypatch, http, mode):  # noqa: F811
    """The real request path: no route handler of either mode is replaced.

    Global admits every request (operator session) before routing; the test
    admits it, as an installed global runtime would, and checks the route.
    """
    _fake_bindings(monkeypatch)
    monkeypatch.setattr(server, mode, True)
    if mode == "GLOBAL_MODE":
        from dashboard import http_operator

        class Admitted:
            def __init__(self, *_a): pass
            def http(self, *_a): return None
        monkeypatch.setattr(http_operator, "Admission", Admitted)

    def forbidden(*_a, **_k):
        raise AssertionError("fresh/global must not compute a legacy binding")
    monkeypatch.setattr(server, "history_binding_row", forbidden)
    status, body = http("/api/agents/LiveClaude/history-binding")
    assert status == 404 and json.loads(body) == {"ok": False, "error": "agent_unavailable"}
    # Not a 501 (fresh: route not connected) or 503 (global: backend failure).
    status, _ = http("/api/agents/Bad%2Fname/history-binding")
    assert status == 404


def test_global_without_admission_is_an_environment_failure_not_the_route(world, monkeypatch, http):  # noqa: F811
    """No operator session (no global install here): 503 from admission, for
    every route alike -- distinct from the route's own 404."""
    monkeypatch.setattr(server, "GLOBAL_MODE", True)
    for path in ("/api/agents/LiveClaude/history-binding", "/api/version"):
        status, body = http(path)
        assert status == 503 and json.loads(body)["ok"] is False, (path, body)
