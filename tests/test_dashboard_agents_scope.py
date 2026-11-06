"""`/api/agents?scope=live`: the live roster without the 30-day history.

A live-roster client polls /api/agents every 3 s and shows only the live rows
(running, or category "agent", never warm-up) -- about 3 % of a 30-day answer
of half a megabyte. `scope=live` returns those rows unchanged plus `retired`,
the names the full answer marks retired, which such a client uses to unpin.
Without `scope` the answer is the full one, with additive project metadata, for the
DECK, the installer, the self-test and every other client.

Both scopes share the one roster computation of a history window
(_SingleFlight key ("agents", days)); only the written answer differs, so a
live request and a full request that wait together must each get their own
shape.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from dashboard import server

ROWS = [
    {"name": "LiveWorker", "category": "agent", "running": True, "act_state": "work",
     "task": "パーサーを直す", "extra": {"nested": [1]}},
    {"name": "LiveWaiter", "category": "agent", "running": True, "act_state": "wait"},
    {"name": "AliveButRetired", "category": "agent", "running": True, "retired_but_alive": True},
    {"name": "LiveSelftest", "category": "agent", "running": True, "program": "agentstack-selftest"},
    {"name": "RunningUnnamed", "category": "unnamed", "running": True},
    {"name": "Warming", "category": "warmup", "running": True},
    {"name": "Finished", "category": "finished", "running": False},
    {"name": "Gone", "category": "gone", "running": False},
    {"name": "OldOne", "category": "retired", "running": False},
    {"name": "OldSelftest", "category": "retired", "running": False, "program": "agentstack-selftest"},
]
LIVE = ["LiveWorker", "LiveWaiter", "AliveButRetired", "LiveSelftest", "RunningUnnamed"]
RETIRED = ["OldOne", "OldSelftest"]


@pytest.fixture
def roster(monkeypatch):
    calls = []

    def fake_build_agents(history_days=server.HISTORY_DAYS_DEFAULT):
        calls.append(history_days)
        time.sleep(fake_build_agents.delay)
        return json.loads(json.dumps(ROWS))  # a fresh list each computation

    fake_build_agents.delay = 0.0
    monkeypatch.setattr(server, "build_agents", fake_build_agents)
    server._HTTP_SINGLE_FLIGHT.clear()
    return fake_build_agents, calls


@pytest.fixture
def http():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield lambda path: urllib.request.urlopen(
            f"http://127.0.0.1:{httpd.server_port}{path}", timeout=30).read()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_without_scope_preserves_full_rows_and_declares_project_metadata(roster, http):
    body = http("/api/agents")
    payload = json.loads(body)
    old = json.dumps({"ts": payload["ts"], "history_days": server.HISTORY_DAYS_DEFAULT,
                      "project_key": server._project_key() or None, "agents": ROWS}, ensure_ascii=False).encode()
    assert body == old
    assert "scope" not in payload and "retired" not in payload


@pytest.mark.parametrize("scope", ["", "all", "LIVE", "live2"])
def test_other_scopes_are_the_full_answer(roster, http, scope):
    assert json.loads(http(f"/api/agents?scope={scope}"))["agents"] == ROWS


def test_live_scope_keeps_the_live_rows_whole_and_lists_the_retired(roster, http):
    payload = json.loads(http("/api/agents?scope=live"))
    assert payload["scope"] == "live"
    assert [row["name"] for row in payload["agents"]] == LIVE
    assert payload["agents"] == [row for row in ROWS if row["name"] in LIVE]  # every field kept
    assert payload["retired"] == RETIRED
    assert payload["history_days"] == server.HISTORY_DAYS_DEFAULT


def test_live_rows_match_the_clients_own_filter(roster, http):
    """The filter a live-roster client applied to the full answer itself."""
    full = json.loads(http("/api/agents"))["agents"]
    client = [r for r in full if (r.get("running") or r.get("category") == "agent")
               and r.get("category") != "warmup"]
    assert json.loads(http("/api/agents?scope=live"))["agents"] == client


def test_retired_follows_the_history_window(roster, http):
    payload = json.loads(http("/api/agents?scope=live&days=7"))
    assert payload["history_days"] == 7.0
    assert roster[1][-1] == 7.0


@pytest.mark.parametrize("order", [("live", "full", "live"), ("full", "live", "full")])
def test_live_and_full_waiting_together_each_get_their_shape(roster, http, order, monkeypatch):
    """The first request computes; the other two arrive while it runs and
    share the next computation. The order is held by events, not by sleeps."""
    _, calls = roster
    entered, gate = threading.Event(), threading.Event()
    real_build = server.build_agents

    def first_one_blocks(history_days=server.HISTORY_DAYS_DEFAULT):
        if not entered.is_set():
            entered.set()
            assert gate.wait(10)
        return real_build(history_days)

    monkeypatch.setattr(server, "build_agents", first_one_blocks)
    results = {}

    def ask(index, scope):
        path = "/api/agents?scope=live" if scope == "live" else "/api/agents"
        results[index] = (scope, json.loads(http(path)))

    threads = [threading.Thread(target=ask, args=(i, s)) for i, s in enumerate(order)]
    started = []
    try:
        threads[0].start()
        started.append(threads[0])
        assert entered.wait(10)  # the first computation is running
        for thread in threads[1:]:
            thread.start()
            started.append(thread)
        flight, key = server._HTTP_SINGLE_FLIGHT, ("agents", server.HISTORY_DAYS_DEFAULT)
        deadline = time.monotonic() + 10
        while True:  # both followers have joined the next computation
            with flight._cond:
                if flight._states.get(key, {}).get("wanted", {}).get(2) == 2:
                    break
            assert time.monotonic() < deadline, "the followers never joined"
            time.sleep(0.005)
    finally:
        # Even when a wait above fails, no request is left behind.
        gate.set()
        for thread in started:
            thread.join(10)
        assert not [t for t in started if t.is_alive()], "a request thread is still running"
    assert len(results) == 3
    for scope, payload in results.values():
        if scope == "live":
            assert payload["scope"] == "live" and [r["name"] for r in payload["agents"]] == LIVE
        else:
            assert "scope" not in payload and payload["agents"] == ROWS
    assert len(calls) == 2  # the running one, then one shared by both followers


def test_different_windows_do_not_share(roster, http):
    build, calls = roster
    build.delay = 0.2
    out = {}
    threads = [threading.Thread(target=lambda d=d: out.__setitem__(d, json.loads(
        http(f"/api/agents?scope=live&days={d}")))) for d in ("7", "30")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert out["7"]["history_days"] == 7.0 and out["30"]["history_days"] == 30.0
    assert sorted(calls) == [7.0, 30.0]


def test_the_shared_payload_is_not_changed_by_a_projection():
    answer = server._AgentsAnswer.__new__(server._AgentsAnswer)
    answer.payload = {"ts": 1, "history_days": 30.0, "project_key": "/fixture/project",
                      "agents": json.loads(json.dumps(ROWS))}
    answer._lock = threading.Lock()
    answer._bodies = {}
    answer.body("live")
    assert answer.payload["agents"] == ROWS
    assert json.loads(answer.body(""))["agents"] == ROWS
    assert answer.body("live") is answer.body("live")  # written once per scope
