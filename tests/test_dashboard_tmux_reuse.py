"""tmux is asked once for what a session keeps for its whole life.

PARENT_AGENT is set when a session is created. The provider-stall sampler (every
5 s) and the graph's lineage read (every 10 s) used to start one tmux process per
session each time to read it again. They now ask once per session generation;
a session that answered "no parent" is asked again after a while. /api/graph is
also reused for a few seconds, and dropped by any POST, like /api/agents.
"""
from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from dashboard import graph_data, server


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(server, "_parent_env_memo", {})
    monkeypatch.setattr(graph_data, "_PARENT_BY_SESSION", {})
    monkeypatch.setattr(graph_data, "_LIVE_PARENT_CACHE", {"ts": 0.0, "value": None})


def test_stall_sampler_reads_a_sessions_parent_once_per_generation(monkeypatch):
    asked = []
    monkeypatch.setattr(server, "_tmux", lambda args: asked.append(args) or "PARENT_AGENT=Boss\n")
    assert server._stall_parent("Kid", "g1") == "Boss"
    assert server._stall_parent("Kid", "g1") == "Boss"
    assert len(asked) == 1 and asked[0][-1] == "PARENT_AGENT"
    assert server._stall_parent("Kid", "g2") == "Boss"  # a new generation is a new session
    assert len(asked) == 2


def test_stall_sampler_asks_again_for_a_session_without_a_parent(monkeypatch):
    answers = iter(["", "PARENT_AGENT=Boss\n"])
    monkeypatch.setattr(server, "_tmux", lambda args: next(answers))
    monkeypatch.setattr(server, "_PARENT_ENV_MISS_SECONDS", 0.0)
    assert server._stall_parent("Kid", "g1") == ""
    assert server._stall_parent("Kid", "g1") == "Boss"


def test_stall_sampler_remembers_a_miss_for_a_while(monkeypatch):
    asked = []
    monkeypatch.setattr(server, "_tmux", lambda args: asked.append(args) or "")
    server._stall_parent("Kid", "g1")
    server._stall_parent("Kid", "g1")
    assert len(asked) == 1


def _fake_tmux(tmp_path, sessions):
    log = tmp_path / "calls"
    script = tmp_path / "tmux"
    rows = "\\n".join(f"{n}\\t{i}\\t{c}" for n, i, c in sessions)
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        'case "$1" in\n'
        f'  list-sessions) printf "{rows}\\n" ;;\n'
        '  show-environment) case "$3" in Kid) echo PARENT_AGENT=Boss ;; *) exit 1 ;; esac ;;\n'
        "esac\n", encoding="utf-8")
    script.chmod(0o755)
    return log


def test_lineage_reads_each_session_once_across_refreshes(tmp_path, monkeypatch):
    log = _fake_tmux(tmp_path, [("Kid", "$1", "10"), ("Loner", "$2", "11")])
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    assert graph_data._live_parents() == {"Kid": "Boss"}
    first = log.read_text().count("show-environment")
    assert first == 2
    graph_data._LIVE_PARENT_CACHE.update(ts=0.0)  # the 10 s refresh comes round
    assert graph_data._live_parents() == {"Kid": "Boss"}
    assert log.read_text().count("show-environment") == first  # nothing asked again


def test_lineage_reads_a_replaced_session_again(tmp_path, monkeypatch):
    log = _fake_tmux(tmp_path, [("Kid", "$1", "10")])
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    graph_data._live_parents()
    _fake_tmux(tmp_path, [("Kid", "$9", "99")])  # same name, a new session
    graph_data._LIVE_PARENT_CACHE.update(ts=0.0)
    graph_data._live_parents()
    assert log.read_text().count("show-environment") == 2


def test_lineage_forgets_a_session_that_is_gone(tmp_path, monkeypatch):
    _fake_tmux(tmp_path, [("Kid", "$1", "10")])
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    graph_data._live_parents()
    _fake_tmux(tmp_path, [("Other", "$2", "11")])
    graph_data._LIVE_PARENT_CACHE.update(ts=0.0)
    graph_data._live_parents()
    assert set(graph_data._PARENT_BY_SESSION) == {"Other"}


@pytest.fixture
def graph_http(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(server, "_GRAPH_REUSE", server._TimedReuse(4.0, lambda: now[0]))
    calls = []
    monkeypatch.setattr(server, "_graph_response",
                        lambda days, show_all, spawn_only: calls.append((days, show_all, spawn_only)) or {"n": len(calls)})
    server._HTTP_SINGLE_FLIGHT.clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def get(path):
        return json.loads(urllib.request.urlopen(base + path, timeout=30).read())

    def post(path):
        request = urllib.request.Request(base + path, data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=30)
        except Exception:
            pass

    try:
        yield get, post, calls, now
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_graph_is_reused_within_the_window_and_per_query(graph_http):
    get, _, calls, now = graph_http
    get("/api/graph?all=1")
    now[0] += 3.9
    assert get("/api/graph?all=1") == {"n": 1}
    get("/api/graph?all=1&spawn_only=1")
    assert len(calls) == 2  # a different query is its own answer
    now[0] += 0.2
    assert get("/api/graph?all=1") == {"n": 3}


def test_a_post_drops_the_graph_answer(graph_http):
    get, post, calls, _ = graph_http
    get("/api/graph")
    post("/api/kill")
    get("/api/graph")
    assert len(calls) == 2


def test_the_stall_monitor_samples_every_ten_seconds_by_default():
    import inspect

    from dashboard import provider_stalls

    assert inspect.signature(provider_stalls.Monitor.run).parameters["interval"].default == 10
