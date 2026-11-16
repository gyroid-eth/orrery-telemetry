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


def test_stall_sampler_never_keeps_an_empty_or_failed_answer(monkeypatch):
    """The monitor settles "no parent" on its second capture and never retries,
    so a parent that shows up late must be seen on the very next sample."""
    answers = iter(["", "", "PARENT_AGENT=Boss\n", "PARENT_AGENT=Boss\n"])
    asked = []
    monkeypatch.setattr(server, "_tmux", lambda args: asked.append(args) or next(answers))
    assert [server._stall_parent("Kid", "g1") for _ in range(4)] == ["", "", "Boss", "Boss"]
    assert len(asked) == 3  # the parent, once found, is kept


def test_a_post_drops_the_remembered_parents(monkeypatch):
    monkeypatch.setattr(server, "_tmux", lambda args: "PARENT_AGENT=Boss\n")
    server._stall_parent("Kid", "g1")
    graph_data._PARENT_BY_SESSION["Kid"] = (("$1", "1"), "Boss", 0.0)
    server.invalidate_display_reuse()
    assert server._parent_env_memo == {} and graph_data._PARENT_BY_SESSION == {}


def _fake_tmux(tmp_path, sessions, pid="100"):
    log = tmp_path / "calls"
    script = tmp_path / "tmux"
    rows = "\\n".join(f"{n}\\t{i}\\t{c}\\t{pid}" for n, i, c in sessions)
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


def test_lineage_notices_a_parent_set_again_after_the_memo_expires(tmp_path, monkeypatch):
    log = _fake_tmux(tmp_path, [("Kid", "$1", "10")])
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    assert graph_data._live_parents() == {"Kid": "Boss"}
    (tmp_path / "tmux").write_text((tmp_path / "tmux").read_text().replace("Kid) echo PARENT_AGENT=Boss", "Kid) echo PARENT_AGENT=Other"))
    graph_data._LIVE_PARENT_CACHE.update(ts=0.0)
    assert graph_data._live_parents() == {"Kid": "Boss"}  # within the memo: kept
    monkeypatch.setattr(graph_data, "_PARENT_MISS_TTL", 0.0)
    graph_data._LIVE_PARENT_CACHE.update(ts=0.0)
    assert graph_data._live_parents() == {"Kid": "Other"}
    assert log.read_text().count("show-environment") == 2


def test_lineage_reads_again_after_the_tmux_server_restarts_with_the_same_ids(tmp_path, monkeypatch):
    """A new server's first session can have the same id and creation second."""
    log = _fake_tmux(tmp_path, [("Kid", "$0", "10")], pid="100")
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    graph_data._live_parents()
    _fake_tmux(tmp_path, [("Kid", "$0", "10")], pid="200")
    graph_data._LIVE_PARENT_CACHE.update(ts=0.0)
    graph_data._live_parents()
    assert log.read_text().count("show-environment") == 2


def test_lineage_without_the_pid_field_still_works(tmp_path, monkeypatch):
    script = tmp_path / "tmux"
    script.write_text('#!/bin/sh\ncase "$1" in\n  list-sessions) printf "Kid\\n" ;;\n'
                      '  show-environment) echo PARENT_AGENT=Boss ;;\nesac\n', encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    assert graph_data._live_parents() == {"Kid": "Boss"}


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


def test_a_clear_racing_the_prune_does_not_break_the_lineage(tmp_path, monkeypatch):
    """forget_parents() from a POST thread may empty the memo while the refresh
    is still pruning the sessions that are gone; two or more used to raise."""
    _fake_tmux(tmp_path, [("Keep", "$1", "10")])
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    graph_data._PARENT_BY_SESSION.update({
        "GoneA": (("$7", "1", "1"), "Boss", 0.0), "GoneB": (("$8", "1", "1"), "Boss", 0.0)})

    class Racing(dict):
        def __iter__(self):
            keys = list(super().keys())
            self.clear()  # the POST thread's clear lands before the pops
            return iter(keys)

    monkeypatch.setattr(graph_data, "_PARENT_BY_SESSION", Racing(graph_data._PARENT_BY_SESSION))
    graph_data._live_parents()
