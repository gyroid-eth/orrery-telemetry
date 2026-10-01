"""The cockpit's graph poll must not cost a per-row recomputation (AUDIT 2026-10-01).

The ORRERY cockpit polls `/api/graph?all=1&spawn_only=1` every 6 s. The
dashboard computed resume capability for every one of ~2,000 rows, opening
SQLite once or twice per row, and then discarded all of it for `spawn_only`.
Overlapping polls contended on the GIL and SQLite's mutex until one request
took over 10 s and the cockpit's 6 s timeout re-fired it.
"""
from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
import pathlib
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import pytest

import dashboard.server as server

ROWS = 2000


@pytest.fixture
def roster(monkeypatch, tmp_path):
    """A production-shaped roster: 2,000 retired Claude and Codex rows."""
    db = tmp_path / "mail.sqlite3"
    names = [f"Agent{index:04d}" for index in range(ROWS)]
    programs = ["claude-code" if index % 2 else "codex-cli" for index in range(ROWS)]
    with sqlite3.connect(db) as con:
        con.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT);
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT, model TEXT,
                program TEXT, task_description TEXT, inception_ts TEXT,
                last_active_ts TEXT, retired_at TEXT
            );
            INSERT INTO projects VALUES (1, 'fixture-project'), (2, 'other-project');
            """
        )
        con.executemany(
            "INSERT INTO agents VALUES (?, 1, ?, 'fixture-model', ?, 'task', "
            "'2026-09-01 00:00:00', '2026-09-22 00:00:00', '2026-09-22 00:01:00')",
            [(index + 1, name, program) for index, (name, program) in enumerate(zip(names, programs))],
        )
        # The same name in another project, more recently active: an
        # unscoped id lookup takes it, a project-scoped one must not.
        con.execute(
            "INSERT INTO agents VALUES (?, 2, 'Agent0001', 'other', 'claude-code', '', "
            "'2026-09-01 00:00:00', '2026-09-30 00:00:00', NULL)",
            (ROWS + 1,),
        )
    spawn = [{"source": names[index], "target": names[index + 1]} for index in range(0, 40, 2)]
    graph = {
        "nodes": [{"name": name, "program": program, "model": "fixture-model",
                   "last_active": 1_790_000_000 + index, "retired": True}
                  for index, (name, program) in enumerate(zip(names, programs))],
        "edges": [], "spawn": spawn,
    }
    (tmp_path / "claude-projects").mkdir()
    claude = tmp_path / "claude"
    claude.write_text("", encoding="utf-8")
    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "CLAUDE_PROJECTS", str(tmp_path / "claude-projects"))
    monkeypatch.setattr(server, "SESSION_INDEX_DIR", str(tmp_path / "session-index"))
    monkeypatch.setattr(server, "ABS_CLAUDE", str(claude))
    monkeypatch.setattr(server, "_raw_graph", lambda: graph)
    for name, value in (("tmux_state", {}), ("_codex_app_runtimes", {}), ("_deliverables_index", {}),
                        ("_name_substitutions", {}), ("_persistent_profiles", {}), ("_annotations", {})):
        monkeypatch.setattr(server, name, lambda value=value: value)
    monkeypatch.setattr(server, "_project_key", lambda: "fixture-project")
    monkeypatch.setattr(server, "_terminal_adapter", lambda: "fixture")
    server._RETIRED_AT_CACHE.clear()
    server._RESUME_CAPABILITY_CACHE.clear()
    if hasattr(server, "_HTTP_SINGLE_FLIGHT"):
        server._HTTP_SINGLE_FLIGHT.clear()
    opened = []
    original = server._db

    def counting_db():
        opened.append(1)
        return original()

    monkeypatch.setattr(server, "_db", counting_db)
    return {"names": names, "spawn": spawn, "opened": opened}


@pytest.fixture
def http():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield lambda path: json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{httpd.server_port}{path}", timeout=30).read())
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_spawn_only_poll_does_no_per_row_work(roster, http, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_resume_capability_for_row", lambda *a, **k: calls.append(a) or "ready")
    started = time.perf_counter()
    payload = http("/api/graph?all=1&spawn_only=1")
    elapsed = time.perf_counter() - started
    assert payload["spawn"] == roster["spawn"] and payload["spawn_only"] is True
    assert payload["nodes"] == [] and payload["edges"] == []
    assert calls == [] and roster["opened"] == []
    assert elapsed < 1.0


def test_spawn_only_keeps_the_recency_window(roster, http):
    """Without all=1 only edges between recently active (or running) rows remain."""
    payload = http("/api/graph?days=0.00001&spawn_only=1")
    full = server.graph_payload(0.00001, False)
    assert payload["spawn"] == full["spawn"]


@pytest.mark.parametrize("surface", ["graph", "agents"])
def test_one_poll_reads_the_registrations_once(roster, surface):
    if surface == "graph":
        rows = server.graph_payload(4, True)["nodes"]
    else:
        rows = server.build_agents(None)
    assert len(rows) == ROWS
    # Before: about one or two connections per row (3,000+ for 2,000 rows).
    assert len(roster["opened"]) <= 10, len(roster["opened"])


def test_batched_lookups_answer_like_the_per_row_ones(roster):
    rows = {row["name"]: row for row in server.graph_payload(4, True)["nodes"]}
    server._RESUME_CAPABILITY_CACHE.clear()
    sample = roster["names"][:50]
    for name in sample:
        program = rows[name]["program"]
        assert rows[name]["resume_capability"] == server._resume_capability(
            name, program, category="retired", verify_transcript=False)
    with server._registration_batch():
        for name in sample + ["Missing"]:
            batched = (server._agent_id_for_name(name), server._claude_registration(name),
                       server._codex_registration(name))
            with server._registration_batch(enabled=False):
                direct = (server._agent_id_for_name(name), server._claude_registration(name),
                          server._codex_registration(name))
            assert batched == direct, name


@pytest.mark.parametrize("path", ["/api/graph?all=1", "/api/agents?days=all"])
def test_overlapping_polls_share_computations_without_getting_older(roster, http, monkeypatch, path):
    """Four polls at once cost two computations, and none gets an older one.

    The first starts a computation; the three that arrive during it wait for
    the next one, which starts after they arrived (DESIGN 4-3).
    """
    started = []
    real_graph, real_agents = server.graph_payload, server.build_agents

    def slow(real):
        def wrapper(*args, **kwargs):
            started.append(time.monotonic())
            time.sleep(0.3)
            return real(*args, **kwargs)
        return wrapper

    monkeypatch.setattr(server, "graph_payload", slow(real_graph))
    monkeypatch.setattr(server, "build_agents", slow(real_agents))
    arrived, results = {}, {}

    def poll(index, delay):
        time.sleep(delay)
        arrived[index] = time.monotonic()
        results[index] = http(path)

    threads = [threading.Thread(target=poll, args=(index, 0 if index == 0 else 0.1)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert len(started) == 2, started
    assert results[1] == results[2] == results[3]
    # Every late request was answered by the computation that began after it arrived.
    assert all(started[1] >= arrived[index] for index in (1, 2, 3))
    # Nothing is kept once served: the next poll computes afresh.
    http(path)
    assert len(started) == 3


def test_a_failed_computation_does_not_fail_the_requests_waiting_for_the_next(roster):
    flight = server._SingleFlight()
    calls = []

    def compute():
        calls.append(1)
        if len(calls) == 1:
            time.sleep(0.2)
            raise RuntimeError("fixture")
        return b"ok"

    errors, bodies = [], []

    def one():
        try:
            bodies.append(flight.get(("k",), compute))
        except RuntimeError as exc:
            errors.append(exc)

    first = threading.Thread(target=one)
    first.start()
    time.sleep(0.05)
    others = [threading.Thread(target=one) for _ in range(2)]
    for thread in others:
        thread.start()
    for thread in [first, *others]:
        thread.join(timeout=10)
    assert len(errors) == 1 and bodies == [b"ok", b"ok"]
    assert flight._states == {}


def without_batching(monkeypatch):
    monkeypatch.setattr(server, "_batched_registrations", lambda: None)


@pytest.mark.parametrize("days,show_all", [(4, True), (0.00001, False)])
def test_graph_payload_is_unchanged_by_the_batched_read(roster, monkeypatch, days, show_all):
    """What NETWORK and the cockpit show stays the same, row for row."""
    batched = server.graph_payload(days, show_all)
    server._RESUME_CAPABILITY_CACHE.clear()
    without_batching(monkeypatch)
    direct = server.graph_payload(days, show_all)
    assert batched == direct


def test_agents_payload_is_unchanged_by_the_batched_read(roster, monkeypatch):
    batched = server.build_agents(None)
    server._RESUME_CAPABILITY_CACHE.clear()
    without_batching(monkeypatch)
    assert server.build_agents(None) == batched


@pytest.mark.parametrize("query", ["all=1", "days=4", "days=0.00001"])
def test_spawn_only_body_is_the_full_payloads_lineage(roster, http, query):
    """spawn_only returns exactly what it returned when cut from the full payload."""
    lineage = http(f"/api/graph?{query}&spawn_only=1")
    full = http(f"/api/graph?{query}")
    for body in (lineage, full):
        body.pop("ts")
    assert lineage == {"nodes": [], "edges": [], "spawn": full["spawn"], "spawn_only": True,
                       "timestamp_diagnostics": full["timestamp_diagnostics"],
                       "degraded": full["degraded"]}


def test_an_always_failing_computation_never_runs_twice_at_once(roster):
    """#161 review P3-2: after a failure each waiter recomputed on its own (up to 4 at once)."""
    flight = server._SingleFlight()
    running, peak, lock = [0], [0], threading.Lock()

    def compute():
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        try:
            time.sleep(0.3)
            raise RuntimeError("fixture")
        finally:
            with lock:
                running[0] -= 1

    errors = []

    def one():
        try:
            flight.get(("k",), compute)
        except RuntimeError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=one) for _ in range(6)]
    for thread in threads:
        thread.start()
        time.sleep(0.02)
    for thread in threads:
        thread.join(timeout=20)
    assert not any(thread.is_alive() for thread in threads)
    assert len(errors) == 6 and peak[0] == 1
    assert flight._states == {}


def test_a_wait_past_the_limit_is_answered_busy(roster, monkeypatch):
    """#161 review P3-1: a waiter had no upper bound."""
    flight = server._SingleFlight()
    monkeypatch.setattr(server._SingleFlight, "WAIT_LIMIT", 0.2)
    release = threading.Event()
    done = []
    leader = threading.Thread(target=lambda: done.append(flight.get(("k",), lambda: release.wait(5) and b"late")))
    leader.start()
    time.sleep(0.05)
    started = time.monotonic()
    with pytest.raises(server._SingleFlightBusy):
        flight.get(("k",), lambda: b"never")
    assert time.monotonic() - started < 2
    release.set()
    leader.join(timeout=10)
    assert done == [b"late"]
    assert flight._states == {}


def test_busy_poll_gets_503_with_retry_after(roster, http, monkeypatch):
    def busy(_key, _compute):
        raise server._SingleFlightBusy()

    monkeypatch.setattr(server._HTTP_SINGLE_FLIGHT, "get", busy)
    for path in ("/api/agents", "/api/graph?all=1&spawn_only=1"):
        with pytest.raises(urllib.error.HTTPError) as caught:
            http(path)
        assert caught.value.code == 503
        assert caught.value.headers["Retry-After"] == "1"
        assert json.loads(caught.value.read()) == {"error": "busy", "retry": True}


@pytest.mark.parametrize("function,endpoint", [("async function tick(){", "'/api/agents?days='"),
                                               ("async function netTick(){", "'/api/graph?'")])
def test_deck_and_network_keep_their_view_when_busy(function, endpoint):
    """A 503 busy must not blank DECK (agents undefined) or drop NETWORK into its error path."""
    html = (pathlib.Path(server.__file__).with_name("index.html")).read_text(encoding="utf-8")
    body = html[html.index(function):]
    body = body[:body.index("\n}\n")]
    fetch = body.index(f"await fetch({endpoint}")
    guard = body.index("if(r.status===503)return", fetch)
    assert guard < body.index("await r.json()", fetch)


def test_a_retry_after_failures_stays_the_only_computation(roster):
    """#166 review P2-1: the retrying waiter left the registry, so a new request ran a second one."""
    flight = server._SingleFlight()
    running, peak, lock = [0], [0], threading.Lock()
    calls, hold = [], threading.Event()

    def compute():
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            calls.append(1)
            call = len(calls)
        try:
            if call <= 2:
                time.sleep(0.2)
                raise RuntimeError("fixture")
            hold.wait(5)
            return b"ok"
        finally:
            with lock:
                running[0] -= 1

    results = []

    def one():
        try:
            results.append(flight.get(("k",), compute))
        except RuntimeError:
            results.append("error")

    first = [threading.Thread(target=one) for _ in range(3)]
    for thread in first:
        thread.start()
        time.sleep(0.02)
    # The first two computations fail; the third (the retry) is held.
    deadline = time.monotonic() + 5
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) == 3
    assert flight._states, "the retrying computation must stay registered"
    late = threading.Thread(target=one)
    late.start()
    time.sleep(0.1)
    hold.set()
    for thread in [*first, late]:
        thread.join(timeout=10)
    assert peak[0] == 1
    assert results.count("error") == 2 and results.count(b"ok") == 2, results
    assert flight._states == {}
