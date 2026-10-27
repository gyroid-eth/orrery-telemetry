"""Provider failures are reported without restarting or typing into the child."""
from __future__ import annotations

import copy
from pathlib import Path
import subprocess
import threading

import pytest

pytestmark = pytest.mark.usefixtures("isolated_provider_stall_runtime")

from dashboard import provider_stalls as stalls

ERROR = "■ Selected model is at capacity. Please try a different model."
IDLE = "\n› \n  sol medium · Context 80% left · ~/project\n"
CAPACITY = ERROR + IDLE
WORK = "• Working (1s • esc to interrupt)\n" + IDLE


def sample(capture=1, text=CAPACITY, **extra):
    return dict(name="ChildCurie", agent_id=2, project_key="/project",
                generation="101:$1:17", parent="ParentNewton", capture_id=capture,
                evidence=stalls.classify(text), **extra)


def test_current_terminal_error_only():
    assert stalls.classify(CAPACITY)["state"] == "capacity"
    assert stalls.classify(ERROR.replace("Please try", "\nPlease try") + IDLE)["state"] == "capacity"
    for text in (
        ERROR + "\n" + WORK,
        ERROR + "\n• All changes are verified.\n" + IDLE,
        "Example: " + CAPACITY,
        "```\n" + ERROR + "\n```\n" + IDLE,
        ERROR + "\n› continue the task\n" + IDLE,
        "■ API Error: overloaded\n" + IDLE,
        ERROR + "\nPress enter to confirm or esc to cancel\n",
    ):
        assert stalls.classify(text)["state"] != "capacity", text
    assert stalls.classify(WORK)["state"] == "work"


@pytest.mark.parametrize("footer", [
    "GPT-6.1 Terra high · Context 52% left · ~/project · weekly 84% left",
    "gpt-6.1-terra high · Context 52% left · ~/project",
    "GPT-5.6 Sol medium · Context 80% left · ~/project",
])
def test_capacity_accepts_model_id_and_display_name_footers(footer):
    text = ERROR + "\n\n› Ask Codex to do anything\n  " + footer + (
        "\n  ? for shortcuts                ⚠ 6 warnings · f2 to view\n")
    assert stalls.classify(text)["state"] == "capacity"


@pytest.mark.parametrize("footer", [
    "GPT-6.1 Terra · Context 52% left",
    "GPT-6.1 Terra Sol high · Context 52% left",
    "Terra GPT-6.1 high · Context 52% left",
])
def test_capacity_rejects_footers_that_are_not_a_model_and_effort(footer):
    assert stalls.classify(ERROR + "\n› \n  " + footer + "\n")["state"] != "capacity"


def test_live_capacity_frame_with_background_terminals_notifies(tmp_path):
    frame = (Path(__file__).parent / "fixtures" / "codex-capacity-background-terminals.txt").read_text()
    assert stalls.classify(frame) == {"state": "capacity", "error": ERROR}
    sent = []
    monitor = stalls.Monitor(tmp_path / "state.json", lambda *args: sent.append(args) or "sent")
    monitor.tick([sample(1, text=frame)], now=100)
    assert not sent
    monitor.tick([sample(2, text=frame)], now=105)
    monitor.tick([sample(3, text=frame)], now=110)
    assert len(sent) == 1


@pytest.mark.parametrize("fixture", ["codex-remote-compact-capacity.txt", "codex-remote-compact-capacity-80.txt"])
def test_live_remote_compact_capacity_frame_notifies(tmp_path, fixture):
    frame = (Path(__file__).parent / "fixtures" / fixture).read_text()
    assert stalls.classify(frame) == {"state": "capacity", "error": ERROR}
    sent = []
    monitor = stalls.Monitor(tmp_path / "state.json", lambda *args: sent.append(args) or "sent")
    monitor.tick([sample(1, text=frame)], now=100)
    assert not sent
    monitor.tick([sample(2, text=frame)], now=105)
    monitor.tick([sample(3, text=frame)], now=110)
    assert len(sent) == 1


@pytest.mark.parametrize("status", ["", "3 background terminals running · /ps to view · /stop to close\n"])
def test_remote_compact_capacity_only_allows_verified_prefix(status):
    error = "■ Error running remote compact task: " + ERROR.removeprefix("■ ")
    assert stalls.classify(error + "\n" + status + IDLE)["state"] == "capacity"
    for text in (
        error + "\n" + WORK + status + IDLE,
        error + "\n• Follow-up response\n" + status + IDLE,
        "```\n" + error + "\n```\n" + status + IDLE,
        "> " + error + "\n" + status + IDLE,
        "Example: " + error + "\n" + status + IDLE,
        error + "\n■ Different failure\n" + status + IDLE,
    ):
        assert stalls.classify(text)["state"] != "capacity", text


@pytest.mark.parametrize("prefix", [
    "Error running compact task: ", "Error: ", "Example: ",
    "Error running remote compact task: Error running remote compact task: ",
])
def test_capacity_does_not_accept_unverified_prefixes(prefix):
    assert stalls.classify("■ " + prefix + ERROR.removeprefix("■ ") + IDLE)["state"] != "capacity"


@pytest.mark.parametrize("message", [
    "stream disconnected before completion", "context window exceeded",
    "Selected model is at capacity. Please try a different model. extra output",
])
def test_remote_compact_other_errors_do_not_count_as_capacity(message):
    assert stalls.classify("■ Error running remote compact task: " + message + IDLE)["state"] != "capacity"


@pytest.mark.parametrize("remote", [False, True])
def test_wrapped_capacity_does_not_join_unrelated_lines(remote):
    error = ERROR if not remote else "■ Error running remote compact task: " + ERROR.removeprefix("■ ")
    wrapped = error.replace("Please try", "Please\n  try")
    for gap in ("\n", "  \n  "):
        assert stalls.classify(error.replace("Please try", "Please" + gap + "try") + IDLE)["state"] == "capacity"
    for text in (
        error.replace("Please try", "Please\n\n  try"),
        error.replace("Please try", "Please\n  unrelated output\n  try"),
        error.replace("Please try", "Please\n> try"),
        error.removesuffix(" model."),
        wrapped + " extra output",
        wrapped + "\n• Follow-up response",
        "```\n" + wrapped + "\n```",
        "> " + wrapped,
        "Example: " + wrapped,
        wrapped.removeprefix("■ "),
    ):
        assert stalls.classify(text + IDLE)["state"] != "capacity", text
    assert stalls.classify(wrapped + "\n" + WORK)["state"] == "work"


@pytest.mark.parametrize("count", [1, 2, 3, 10, 123])
def test_capacity_allows_one_exact_background_terminal_summary(count):
    plural = "" if count == 1 else "s"
    status = f"{count} background terminal{plural} running · /ps to view · /stop to close"
    assert stalls.classify(ERROR + "\n  " + status + IDLE)["state"] == "capacity"


@pytest.mark.parametrize("intervening", [
    "0 background terminals running · /ps to view · /stop to close",
    "1 background terminals running · /ps to view · /stop to close",
    "2 background terminal running · /ps to view · /stop to close",
    "2 background terminals running · /ps to view",
    "2 background terminals running · /ps to view · /stop to close; arbitrary output",
    "• Queued follow-up inputs\n  ↳ fixture follow-up",
    "/ps\nBackground terminals\n  • fixture process",
    "1 background terminal running · /ps to view · /stop to close\n"
    "1 background terminal running · /ps to view · /stop to close",
])
def test_capacity_rejects_other_lines_before_prompt(intervening):
    assert stalls.classify(ERROR + "\n" + intervening + IDLE)["state"] != "capacity"


def test_background_summary_does_not_hide_work_history_or_quoted_errors():
    status = "3 background terminals running · /ps to view · /stop to close\n"
    for text in (
        ERROR + "\n" + WORK + status + IDLE,
        ERROR + "\n• Follow-up response\n" + status + IDLE,
        "```\n" + ERROR + "\n" + status + "```\n" + IDLE,
    ):
        assert stalls.classify(text)["state"] != "capacity"


def test_two_captures_one_notice_restart_then_work_rearms(tmp_path):
    sent = []
    path = tmp_path / "runtime" / "provider-stalls.json"
    monitor = stalls.Monitor(path, lambda row, event: sent.append(copy.deepcopy(event)) or "sent")
    monitor.tick([sample(1)], now=100)
    monitor.tick([sample(1)], now=105)  # cached capture is not a second observation
    assert sent == []
    monitor.tick([sample(2)], now=110)
    assert len(sent) == 1
    assert monitor.view("ChildCurie", "101:$1:17")["notification_status"] == "sent"
    for n in range(3, 6):
        monitor.tick([sample(n)], now=110 + n)
    restarted = stalls.Monitor(path, lambda row, event: sent.append(event) or "sent")
    restarted.tick([sample(6)], now=120)
    restarted.tick([sample(7, text="")], now=125)  # unknown does not rearm
    restarted.tick([sample(8, text=IDLE)], now=130)  # idle does not rearm
    restarted.tick([sample(9)], now=135)
    assert len(sent) == 1
    restarted.tick([sample(10, text=WORK)], now=140)
    assert restarted.view("ChildCurie", "101:$1:17") is None
    restarted.tick([sample(11)], now=145)
    restarted.tick([sample(12)], now=150)
    assert len(sent) == 2
    assert sent[0]["event_id"] != sent[1]["event_id"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert "registration_token" not in path.read_text()


def test_parentless_has_marker_but_no_message(tmp_path):
    sent = []
    monitor = stalls.Monitor(tmp_path / "state.json", lambda *args: sent.append(args))
    row = sample()
    row["parent"] = ""
    monitor.tick([row], now=100)
    row["capture_id"] = 2
    monitor.tick([row], now=105)
    assert sent == []
    assert monitor.view(row["name"], row["generation"])["notification_status"] == "no_parent"


@pytest.mark.parametrize("result", ["delivery_unknown", "failed", "parent_unverified"])
def test_delivery_errors_are_not_retried(tmp_path, result):
    sent = []
    monitor = stalls.Monitor(tmp_path / "state.json", lambda *args: sent.append(args) or result)
    for n in range(1, 6):
        monitor.tick([sample(n)], now=100 + n * 5)
    assert len(sent) == 1
    assert monitor.view("ChildCurie", "101:$1:17")["notification_status"] == result


def test_generation_change_and_two_monitors_do_not_duplicate(tmp_path):
    sent = []
    path = tmp_path / "state.json"
    first = stalls.Monitor(path, lambda *args: sent.append(args) or "sent")
    second = stalls.Monitor(path, lambda *args: sent.append(args) or "sent")
    first.tick([sample(1)], now=100)
    second.tick([sample(2)], now=105)
    first.tick([sample(3)], now=110)
    assert len(sent) == 1
    row = sample(4)
    row["generation"] = "101:$2:18"
    first.tick([row], now=115)
    row["capture_id"] = 5
    second.tick([row], now=120)
    assert len(sent) == 2


def test_missing_capture_breaks_confirmation_without_clearing_stall(tmp_path):
    sent = []
    monitor = stalls.Monitor(tmp_path / "state.json", lambda *a: sent.append(a) or "sent")
    monitor.tick([sample(1)], now=100)
    monitor.tick([sample(2, text="")], now=105)
    monitor.tick([sample(3)], now=110)
    assert not sent
    monitor.tick([sample(4)], now=115)
    assert len(sent) == 1


def test_worker_runs_without_http_requests(tmp_path):
    sent = threading.Event()
    count = iter(range(1, 1000))
    monitor = stalls.Monitor(tmp_path / "state.json", lambda *a: sent.set() or "sent")
    stop = threading.Event()
    worker = threading.Thread(target=monitor.run, args=(lambda: [sample(next(count))], stop),
                              kwargs={"interval": .02})
    worker.start()
    try:
        assert sent.wait(2)
    finally:
        stop.set()
        worker.join(2)
    assert not worker.is_alive()


def test_isolated_real_tmux_capture(tmp_path):
    """No default tmux socket or agent panes are read in this integration check."""
    import shutil
    if not shutil.which("tmux"):
        pytest.skip("tmux unavailable")
    import os
    import tempfile
    directory = tempfile.TemporaryDirectory(prefix="stall-tmux-", dir="/private/tmp")
    socket = directory.name + "/s"
    env = {**os.environ, "HOME": str(tmp_path), "TMUX": ""}
    def tmux(*args):
        return subprocess.run(["tmux", "-f", "/dev/null", "-S", socket, *args], capture_output=True,
                              text=True, timeout=5, check=True, env=env)
    screen = tmp_path / "screen.txt"
    screen.write_text(CAPACITY)
    tmux("new-session", "-d", "-s", "FixtureCurie", "-x", "130", "-y", "20",
         "cat " + str(screen) + "; sleep 10")
    try:
        import time
        end = time.monotonic() + 2
        text = ""
        while time.monotonic() < end:
            text = tmux("capture-pane", "-p", "-J", "-t", "FixtureCurie").stdout
            if "Selected model" in text:
                break
            time.sleep(.02)
        assert stalls.classify(text)["state"] == "capacity"
    finally:
        tmux("kill-server")
        directory.cleanup()


@pytest.mark.parametrize("width", [60, 80, 100, 120])
@pytest.mark.parametrize("remote", [False, True])
def test_capacity_in_real_pane_after_codex_wrap(tmp_path, monkeypatch, width, remote):
    """Codex's explicit history newlines survive capture-pane -J."""
    import os
    import shlex
    import shutil
    import sys
    import tempfile
    import textwrap
    import time
    from dashboard import server

    if not shutil.which("tmux"):
        pytest.skip("tmux unavailable")
    error = ERROR if not remote else "■ Error running remote compact task: " + ERROR.removeprefix("■ ")
    wrapped = textwrap.fill(error, width=width, subsequent_indent="  ")
    frame = tmp_path / "frame.txt"
    frame.write_text(wrapped + IDLE)
    if remote and width == 80:
        fixture = Path(__file__).parent / "fixtures" / "codex-remote-compact-capacity-80.txt"
        assert fixture.read_text().startswith(wrapped + "\n")
    env = {**os.environ, "HOME": str(tmp_path), "TMUX": ""}
    run = subprocess.run
    with tempfile.TemporaryDirectory(prefix="stall-wrap-", dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as directory:
        socket = directory + "/s"

        def tmux(*args):
            return run(["tmux", "-f", "/dev/null", "-S", socket, *args],
                capture_output=True, text=True, timeout=5, check=True, env=env)

        writer = "import pathlib,sys,time; print(pathlib.Path(sys.argv[1]).read_text(),flush=True); time.sleep(10)"
        try:
            tmux("new-session", "-d", "-s", "FixtureWrap", "-x", str(width), "-y", "24",
                shlex.join([sys.executable, "-c", writer, str(frame)]))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                captured = tmux("capture-pane", "-p", "-J", "-t", "FixtureWrap").stdout
                if "sol medium" in captured:
                    break
                time.sleep(.02)
            assert tmux("display-message", "-p", "-t", "FixtureWrap", "#{pane_width}").stdout.strip() == str(width)
            # Explicit Codex-style newlines survive -J; terminal wraps join.
            assert wrapped in captured
            assert len(wrapped.splitlines()) == (2 if len(error) > width else 1)

            # Bind the owned real endpoint to the normal shared consumer.
            # Capture/admission/PID-start checks must use the same server;
            # a capture-only subprocess mock would skip its transport proof.
            server_pid = tmux("display-message", "-p", "#{pid}").stdout.strip()
            monkeypatch.setenv("HOME", str(tmp_path))
            monkeypatch.setenv("AGENTSTACK_HOME", str(tmp_path / ".agentstack"))
            monkeypatch.setenv("AGENTSTACK_LABEL_PREFIX", "org.agentstack.test.stall-wrap")
            monkeypatch.setenv("TMUX", socket + "," + server_pid + ",0")
            monkeypatch.delenv("TMUX_PANE", raising=False)
            monkeypatch.setattr(server, "_rt_cache", {})
            monkeypatch.setattr(server, "_provider_captures", {})
            server._agent_runtime("FixtureWrap", 101, "$1")
            assert server._provider_captures["FixtureWrap"][2]["state"] == "capacity"
        finally:
            run(["tmux", "-S", socket, "kill-server"], capture_output=True, timeout=5, env=env)


def test_sampler_uses_shared_capture_and_session_parent(monkeypatch):
    from dashboard import server
    session = dict(created=101, session_id="$1", pane_pid=17)
    monkeypatch.setattr(server, "tmux_state", lambda: {"ChildCurie": session})
    monkeypatch.setattr(server, "agentmail_state", lambda: ({"ChildCurie": {"program": "codex"}}, {}))
    monkeypatch.setattr(server, "_process_tree_snapshot", lambda: {})
    monkeypatch.setattr(server, "_tmux_row_state", lambda *a: ("codex", "agent", True, None))
    monkeypatch.setattr(server, "_codex_registration", lambda name: dict(agent_id=2, project_key="/project"))
    monkeypatch.setattr(server, "_batched_registrations", lambda: None)
    monkeypatch.setattr(server, "_agent_runtime", lambda *a: {})
    monkeypatch.setattr(server, "_tmux", lambda argv: "PARENT_AGENT=ParentNewton\n")
    monkeypatch.setattr(server, "_provider_captures", {"ChildCurie": ("101:$1", 10, stalls.classify(CAPACITY))})
    rows = server._provider_stall_samples()
    assert rows == [sample(10)]


@pytest.mark.parametrize("parent", ["ParentNewton", "DifferentParent"])
def test_sender_checks_child_owner_and_parent_before_existing_mail_helper(tmp_path, monkeypatch, parent):
    from dashboard import server
    from types import SimpleNamespace
    row = sample(2)
    session = dict(created=101, session_id="$1", pane_pid=17)
    monkeypatch.setattr(server, "tmux_state", lambda: {"ChildCurie": session})
    monkeypatch.setattr(server, "_tmux", lambda a: "PARENT_AGENT=ParentNewton")
    monkeypatch.setattr(server, "_codex_registration", lambda n: dict(agent_id=2, project_key="/project"))
    monkeypatch.setattr(server, "_resume_registration", lambda *a: {"agent_id": 1})
    monkeypatch.setattr(server, "_runtime_agent_token", lambda n: "fixture-owner")
    monkeypatch.setattr(server, "_child_resume_module", lambda: SimpleNamespace(
        _load_state=lambda p: dict(parent_agent=parent, registration_token="fixture-owner"),
        _validate_identity=lambda *a, **k: None))
    calls = []
    monkeypatch.setattr(server, "_mcp_call", lambda *a, **k: calls.append((a, k)) or {"ok": True})
    event = dict(since=100, event_id="event-fixture", error=ERROR)
    status = server._provider_stall_send(row, event)
    if parent != row["parent"]:
        assert status == "parent_unverified"
        assert not calls
    else:
        assert status == "sent"
        assert calls[0][0][0] == "send_message"
        args = calls[0][0][1]
        assert args["sender_name"] == row["name"] and args["to"] == [row["parent"]]
        assert args["body_md"].startswith("telemetry の自動検知")
        assert "JST" in args["body_md"] and ERROR in args["body_md"]


def test_global_does_not_start_legacy_worker(monkeypatch):
    from dashboard import server
    monkeypatch.setattr(server, "GLOBAL_MODE", True)
    monkeypatch.setattr(server, "_provider_monitor", None)
    monkeypatch.setattr(server.threading, "Thread", lambda **kw: pytest.fail("legacy worker in global"))
    server._start_provider_stall_monitor()
    assert server._provider_monitor is None


def test_long_dashboard_outage_does_not_repeat_same_incident(tmp_path):
    sent = []
    path = tmp_path / "state.json"
    monitor = stalls.Monitor(path, lambda *args: sent.append(args) or "sent")
    monitor.tick([sample(1)], now=100)
    monitor.tick([sample(2)], now=105)
    monitor.tick([], now=100 + 10 * 86400)
    monitor = stalls.Monitor(path, lambda *args: sent.append(args) or "sent")
    monitor.tick([sample(3)], now=110 + 10 * 86400)
    monitor.tick([sample(4)], now=115 + 10 * 86400)
    assert len(sent) == 1


def test_legacy_worker_lifecycle_is_bound_to_dashboard(tmp_path, monkeypatch):
    from dashboard import server
    monkeypatch.setattr(server, "GLOBAL_MODE", False)
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(server, "_provider_monitor", None)
    monkeypatch.setattr(server, "_provider_thread", None)
    monkeypatch.setattr(server, "_provider_stop", threading.Event())
    monkeypatch.setattr(server, "_provider_stall_samples", lambda: [])
    server._start_provider_stall_monitor()
    first = server._provider_thread
    try:
        assert first.is_alive()
        server._start_provider_stall_monitor()
        assert server._provider_thread is first
    finally:
        server._stop_provider_stall_monitor()
    assert not first.is_alive()


def test_import_does_not_start_worker_with_an_inherited_runtime(tmp_path):
    import os
    from pathlib import Path
    import sys
    forbidden = tmp_path / "must-not-be-created"
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTSTACK_")}
    env.update(HOME=str(tmp_path), AGENTSTACK_RUNTIME_DIR=str(forbidden),
               AGENTSTACK_MAIL_DB=str(tmp_path / "fixture.sqlite3"),
               AGENTSTACK_RUNTIME_MODE="legacy")
    code = "import dashboard.server as s; assert s._provider_monitor is None; assert s._provider_thread is None"
    subprocess.run([sys.executable, "-c", code], env=env,
                   cwd=Path(__file__).resolve().parents[1], timeout=10, check=True)
    assert not forbidden.exists()


def test_fixture_replaces_import_time_runtime(isolated_provider_stall_runtime):
    import os
    from dashboard import server
    assert server.RUNTIME_DIR == str(isolated_provider_stall_runtime)
    assert os.environ["AGENTSTACK_RUNTIME_DIR"] == server.RUNTIME_DIR
    assert "AGENTSTACK_CLIENT_CONFIG" not in os.environ


def test_idle_monitor_creates_no_state_or_lock(tmp_path):
    path = tmp_path / "runtime" / "provider-stalls.json"
    monitor = stalls.Monitor(path, lambda *args: pytest.fail("idle send"))
    monitor.tick([], now=100)
    monitor.tick([sample(1, text=IDLE)], now=105)
    monitor.tick([sample(2, text=WORK)], now=110)
    assert not path.exists()
    assert not path.with_suffix(".lock").exists()


def test_unchanged_stall_does_not_rewrite_or_fsync(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monitor = stalls.Monitor(path, lambda *args: "sent")
    monitor.tick([sample(1)], now=100)
    monitor.tick([sample(2)], now=105)
    before = (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
    monkeypatch.setattr(stalls.os, "fsync", lambda fd: pytest.fail("unchanged fsync"))
    monitor.tick([sample(3)], now=110)
    assert (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes()) == before


@pytest.mark.parametrize("names, pruned", [(set(), True), ({"ChildCurie"}, False), (None, False)])
def test_expired_absent_generation_is_pruned_only_with_proof(tmp_path, names, pruned):
    path = tmp_path / "state.json"
    monitor = stalls.Monitor(path, lambda *args: "sent", session_names=lambda: names)
    monitor.tick([sample(1)], now=100)
    monitor.tick([sample(2)], now=105)
    monitor.tick([], now=100 + 31 * 86400)
    assert (monitor.view("ChildCurie", "101:$1:17") is None) == pruned


@pytest.mark.parametrize("response, expected", [
    ({"ok": True, "result": {"isError": True, "content": [{"type": "text", "text": "Error calling tool 'send_message': Recipient is not accepting messages."}]}}, "failed"),
    ({"ok": True, "result": {"isError": True, "content": [{"type": "text", "text": "Error calling tool 'send_message': OS error: fixture archive failure after database commit"}]}}, "delivery_unknown"),
    ({"ok": False, "error": "timeout"}, "delivery_unknown"),
    ({"ok": True, "result": None}, "delivery_unknown"),
])
def test_explicit_mail_rejection_differs_from_unknown_delivery(monkeypatch, response, expected):
    from dashboard import server
    from types import SimpleNamespace
    monkeypatch.setattr(server, "tmux_state", lambda: {"ChildCurie": dict(created=101, session_id="$1", pane_pid=17)})
    monkeypatch.setattr(server, "_tmux", lambda a: "PARENT_AGENT=ParentNewton")
    monkeypatch.setattr(server, "_codex_registration", lambda n: dict(agent_id=2, project_key="/project"))
    monkeypatch.setattr(server, "_resume_registration", lambda *a: {"agent_id": 1})
    monkeypatch.setattr(server, "_runtime_agent_token", lambda n: "fixture-owner")
    monkeypatch.setattr(server, "_child_resume_module", lambda: SimpleNamespace(
        _load_state=lambda p: dict(parent_agent="ParentNewton", registration_token="fixture-owner"),
        _validate_identity=lambda *a, **k: None))
    monkeypatch.setattr(server, "_mcp_tool_parameters", lambda t: None)
    monkeypatch.setattr(server, "_mcp_jsonrpc", lambda *a: response)
    assert server._provider_stall_send(sample(2), dict(since=100, event_id="fixture", error=ERROR)) == expected


@pytest.mark.parametrize("code, rejected", [
    (-32600, True), (-32601, True), (-32602, True),
    (-32603, False), (-32000, False), (None, False), ([], False),
])
def test_jsonrpc_only_pre_dispatch_errors_are_definite(monkeypatch, code, rejected):
    import io
    import json
    from dashboard import server
    monkeypatch.setattr(server, "_legacy_mail_bearer_enabled", lambda: False)
    body = json.dumps({"jsonrpc": "2.0", "error": {"code": code, "message": "fixture"}}).encode()
    monkeypatch.setattr(server.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(
        body))
    assert server._mcp_jsonrpc("tools/call", {})["rejected"] is rejected


def test_delayed_work_cannot_rearm_a_newer_stalled_episode(tmp_path):
    sent = []
    path = tmp_path / "state.json"
    send = lambda r, e: sent.append(e["event_id"]) or "sent"
    a, b = stalls.Monitor(path, send), stalls.Monitor(path, send)
    a.tick([sample(10)], now=100)
    a.tick([sample(20)], now=105)
    b.tick([sample(15, text=WORK)], now=106)
    assert b.view("ChildCurie", "101:$1:17") is not None
    a.tick([sample(30)], now=110)
    a.tick([sample(40)], now=115)
    assert len(sent) == 1


def test_older_capacity_cannot_confirm_newer_candidate(tmp_path):
    attempts = []
    monitor = stalls.Monitor(tmp_path / "state.json", lambda *args: attempts.append(args) or "sent")
    monitor.tick([sample(200)], now=200)
    monitor.tick([sample(100)], now=205)
    assert not attempts
    monitor.tick([sample(300)], now=210)
    assert len(attempts) == 1


def test_rearmed_episode_keeps_its_capture_floor_across_restart(tmp_path):
    path = tmp_path / "state.json"
    sent = []
    send = lambda *args: sent.append(args) or "sent"
    monitor = stalls.Monitor(path, send)
    monitor.tick([sample(10)], now=100)
    monitor.tick([sample(20)], now=105)
    monitor.tick([sample(30, text=WORK)], now=110)
    monitor = stalls.Monitor(path, send)
    monitor.tick([sample(21)], now=115)
    monitor.tick([sample(22)], now=120)
    assert len(sent) == 1
    monitor.tick([sample(40)], now=125)
    monitor.tick([sample(50)], now=130)
    assert len(sent) == 2


def test_work_after_clear_advances_shared_capture_floor(tmp_path):
    path = tmp_path / "state.json"
    sent = []
    send = lambda *args: sent.append(args) or "sent"
    a, b = stalls.Monitor(path, send), stalls.Monitor(path, send)
    a.tick([sample(10)], now=100)
    a.tick([sample(20)], now=105)
    a.tick([sample(30, text=WORK)], now=110)
    # Clear is already persisted; later work must still reach other monitors.
    a.tick([sample(50, text=WORK)], now=115)
    b.tick([sample(40)], now=120)
    b.tick([sample(45)], now=125)
    assert len(sent) == 1
    assert b.view("ChildCurie", "101:$1:17") is None
    restarted = stalls.Monitor(path, send)
    restarted.tick([sample(46)], now=130)
    restarted.tick([sample(47)], now=135)
    assert len(sent) == 1
    # A genuinely newer stop can still be notified.
    restarted.tick([sample(60)], now=140)
    restarted.tick([sample(70)], now=145)
    assert len(sent) == 2


@pytest.mark.parametrize("history", ["never_candidate", "candidate", "stalled"])
def test_work_writes_capture_order_only_for_tracked_children(tmp_path, monkeypatch, history):
    path = tmp_path / "state.json"
    monitor = stalls.Monitor(path, lambda *args: "sent")
    if history == "never_candidate":
        # Another child's journal must not make this working child writable.
        other = sample(1)
        other.update(name="OtherChild", agent_id=3, generation="102:$2:18")
        monitor.tick([other], now=100)
    else:
        monitor.tick([sample(10)], now=100)
        if history == "stalled":
            monitor.tick([sample(20)], now=105)
        monitor.tick([sample(30, text=WORK)], now=110)
    writes = []
    replace = stalls.os.replace

    def counted_replace(source, destination):
        if destination == path:
            writes.append(destination)
        return replace(source, destination)

    monkeypatch.setattr(stalls.os, "replace", counted_replace)
    for capture in range(31, 41):
        monitor.tick([sample(capture, text=WORK)], now=110 + capture)
    assert len(writes) == (0 if history == "never_candidate" else 10)


@pytest.mark.parametrize("case", [
    "postcommit_os", "postcommit_value", "argument_unknown", "argument_type",
    "contact_blocked", "contact_required", "sender_token",
])
def test_real_mail_rejection_contract_and_postcommit_uncertainty(tmp_path, monkeypatch, case):
    """Use real FastMCP errors: isError alone does not establish non-delivery."""
    import asyncio
    import sqlite3
    from fastmcp import Client
    from agentstack_mail import app, config, db
    from dashboard import server

    database = tmp_path / "mail.sqlite3"
    project = str(tmp_path / "project")
    for key, value in {
        "AGENTSTACK_MAIL_ENV_FILE": str(tmp_path / "missing.env"),
        "AGENTSTACK_MAIL_DATABASE_URL": "sqlite+aiosqlite:///" + str(database),
        "AGENTSTACK_MAIL_DB": str(database),
        "AGENTSTACK_MAIL_STORAGE_ROOT": str(tmp_path / "archive"),
        "AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR": str(tmp_path / "signals"),
        "AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED": "false",
        "AGENTSTACK_MAIL_TOOLS_LOG_ENABLED": "false",
        "AGENTSTACK_MAIL_CONTACT_ENFORCEMENT_ENABLED": "true",
        "AGENTSTACK_MAIL_MESSAGING_AUTO_HANDSHAKE_ON_BLOCK": "false",
    }.items():
        monkeypatch.setenv(key, value)
    db.reset_database_state()
    config.clear_settings_cache()

    async def run():
        async with Client(app.build_mcp_server()) as client:
            await client.call_tool("ensure_project", {"human_key": project})
            agents = []
            for name in ("ChildCurie", "ParentNewton"):
                result = await client.call_tool("register_agent", dict(
                    project_key=project, name=name, program="codex", model="fixture"))
                agents.append(result.structured_content or result.data)
            for agent in agents:
                policy = "open"
                if agent is agents[1]:
                    policy = {"contact_blocked": "block_all", "contact_required": "contacts_only"}.get(case, "open")
                await client.call_tool("set_contact_policy", dict(
                    project_key=project, agent_name=agent["name"], policy=policy))
            args = dict(project_key=project, sender_name=agents[0]["name"],
                to=[agents[1]["name"]], subject="capacity notice", body_md="fixture body",
                auto_contact_if_blocked=False)
            if case == "argument_unknown":
                args["unknown_argument"] = "fixture"
            elif case == "argument_type":
                args["subject"] = 123
            elif case == "sender_token":
                args["sender_token"] = "different-test-credential"
            if case.startswith("postcommit_"):
                async def fail_archive(*a, **kw):
                    error = OSError if case == "postcommit_os" else ValueError
                    raise error("fixture archive failure after database commit")
                monkeypatch.setattr(app, "write_message_bundle", fail_archive)
            result = await client.call_tool("send_message", args, raise_on_error=False)
            assert result.is_error
            return dict(isError=result.is_error,
                content=[block.model_dump(mode="json") for block in result.content])

    try:
        payload = asyncio.run(run())
        monkeypatch.setattr(server, "_mcp_tool_parameters", lambda *a: None)
        monkeypatch.setattr(server, "_mcp_jsonrpc", lambda *a, **kw: dict(ok=True, result=payload))
        interpreted = server._mcp_call("send_message", {})
        assert interpreted["ok"] is False
        assert interpreted["rejected"] is (not case.startswith("postcommit_")), payload
        with sqlite3.connect(database) as connection:
            expected = int(case.startswith("postcommit_"))
            assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == expected
            assert connection.execute("SELECT COUNT(*) FROM message_recipients").fetchone()[0] == expected
    finally:
        db.reset_database_state()
        config.clear_settings_cache()
