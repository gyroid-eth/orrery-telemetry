"""Submitted input followed immediately by capacity is a new stop, not work."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from types import SimpleNamespace

import pytest

from dashboard import provider_stalls as stalls

pytestmark = pytest.mark.usefixtures("isolated_provider_stall_runtime")

ERROR = "■ Selected model is at capacity. Please try a different model."
CHROME = "\n\n  3 background terminals running · /ps to view · /stop to close\n\n› Ask Codex to do anything\n  sol medium · Context 36% left · ~/project\n  ? for shortcuts\n"
# Saved capacity pane, with task text, commands, paths, identities and model
# redacted. Retry turns below are synthetic; no real conversation is retained.
FRAME = (Path(__file__).parent / "fixtures/codex-capacity-rearm-redacted.txt").read_text()
HISTORY = FRAME[:FRAME.rindex(ERROR) + len(ERROR)]


def retry(history=HISTORY, prompt="Continue the fixture task"):
    return history + "\n\n› " + prompt + "\n\n" + ERROR


def sample(capture, text=FRAME, *, name="ChildCurie", agent_id=2):
    # Before the fix, keep the observation path executable to prove the lost
    # second notification, rather than failing just because a helper is absent.
    observe = getattr(stalls, "observe", stalls.classify)
    return dict(name=name, agent_id=agent_id, project_key="/project",
                generation="101:$1:17", parent="ParentNewton", capture_id=capture,
                evidence=observe(text))


def start(path):
    sent = []
    send = lambda row, event: sent.append(copy.deepcopy(event)) or "sent"
    monitor = stalls.Monitor(path, send)
    monitor.tick([sample(10)], now=100)
    monitor.tick([sample(20)], now=105)
    assert len(sent) == 1
    return monitor, sent, send


def test_submitted_input_then_immediate_capacity_notifies_after_two_captures(tmp_path):
    monitor, sent, _ = start(tmp_path / "state.json")
    frame = retry() + CHROME
    assert stalls.classify(frame)["state"] == "capacity"
    monitor.tick([sample(30, frame)], now=110)
    monitor.tick([sample(30, frame)], now=115)
    assert len(sent) == 1
    monitor.tick([sample(40, frame)], now=120)
    assert len(sent) == 2
    assert sent[0]["event_id"] != sent[1]["event_id"]
    for n in range(41, 51):
        monitor.tick([sample(n, frame)], now=120 + n)
    assert len(sent) == 2


def test_same_prompt_can_be_submitted_twice_without_work(tmp_path):
    monitor, sent, _ = start(tmp_path / "state.json")
    history = HISTORY
    for capture in (30, 50):
        history = retry(history)
        monitor.tick([sample(capture, history + CHROME)], now=capture + 100)
        monitor.tick([sample(capture + 10, history + CHROME)], now=capture + 105)
    assert len(sent) == 3
    assert len({event["event_id"] for event in sent}) == 3


def test_restart_and_second_monitor_share_retry_boundary(tmp_path):
    path = tmp_path / "state.json"
    a, sent, send = start(path)
    frame = retry() + CHROME
    a.tick([sample(30, frame)], now=110)
    b = stalls.Monitor(path, send)
    b.tick([sample(30, frame)], now=115)
    assert len(sent) == 1
    b.tick([sample(40, frame)], now=120)
    a.tick([sample(50, frame)], now=125)
    stalls.Monitor(path, send).tick([sample(60, frame)], now=130)
    assert len(sent) == 2


@pytest.mark.parametrize("result", ["failed", "delivery_unknown", "parent_unverified"])
def test_retry_is_claimed_once_even_when_delivery_is_uncertain(tmp_path, result):
    path = tmp_path / "state.json"
    monitor, sent, _ = start(path)
    monitor.send = lambda row, event: sent.append(event) or result
    frame = retry() + CHROME
    for capture in range(30, 36):
        monitor.tick([sample(capture, frame)], now=100 + capture)
    assert len(sent) == 2
    assert monitor.view("ChildCurie", "101:$1:17")["notification_status"] == result


@pytest.mark.parametrize("frame", [
    FRAME,  # same error row repainted, even after an arbitrary delay
    HISTORY + CHROME.replace("36%", "35%"),
    HISTORY + CHROME.replace("3 background", "4 background"),
    HISTORY + "\n\n› Continue the fixture task\n  sol medium · Context 36% left\n",  # unsubmitted composer
    HISTORY + "\n\n" + ERROR + CHROME,  # another error row, no input
    HISTORY + "\n\n• Output changed\n\n" + ERROR + CHROME,
    HISTORY + "\n\n> › Continue the fixture task\n\n" + ERROR + CHROME,
    HISTORY + "\n\n```\n› Continue the fixture task\n\n" + ERROR + "\n```\n\n" + ERROR + CHROME,
    HISTORY + "\n\n• Queued follow-up inputs\n  ↳ Continue the fixture task\n\n" + ERROR + CHROME,
])
def test_no_new_submitted_input_means_no_second_notice(tmp_path, frame):
    monitor, sent, _ = start(tmp_path / "state.json")
    for capture in range(30, 35):
        monitor.tick([sample(capture, frame)], now=100 + capture)
    assert len(sent) == 1


def test_delivery_record_alone_does_not_rearm(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("AGENTSTACK_RUNTIME_DIR", str(runtime))
    monitor, sent, _ = start(runtime / "state.json")
    (runtime / "notify-state.json").write_text(json.dumps({"ChildCurie:22": {
        "agent": "ChildCurie", "msg_key": "22", "last_result": "success",
        "last_success_epoch": 110, "source": "watcher"}}))
    monitor.tick([sample(30)], now=115)
    monitor.tick([sample(40)], now=120)
    assert len(sent) == 1


def test_other_child_input_cannot_rearm_this_child(tmp_path):
    monitor, sent, _ = start(tmp_path / "state.json")
    other = retry() + CHROME
    for capture in (30, 40):
        monitor.tick([sample(capture), sample(capture, other, name="OtherCurie", agent_id=3)], now=100 + capture)
    # One initial event per child, no second event for ChildCurie.
    assert len(sent) == 2
    assert len([event for event in sent if event["name"] == "ChildCurie"]) == 1


def test_old_work_or_capacity_cannot_undo_or_confirm_retry(tmp_path):
    path = tmp_path / "state.json"
    a, sent, send = start(path)
    b = stalls.Monitor(path, send)
    frame = retry() + CHROME
    a.tick([sample(200, frame)], now=110)
    b.tick([sample(100, "• Working (1s • esc to interrupt)\n› \n")], now=115)
    b.tick([sample(150, frame)], now=120)
    assert len(sent) == 1
    b.tick([sample(300, frame)], now=125)
    assert len(sent) == 2
    a.tick([sample(250)], now=130)
    a.tick([sample(400, frame)], now=135)
    assert len(sent) == 2


def test_intervening_unknown_cancels_retry_confirmation(tmp_path):
    monitor, sent, _ = start(tmp_path / "state.json")
    frame = retry() + CHROME
    monitor.tick([sample(30, frame)], now=110)
    monitor.tick([sample(40, "")], now=115)
    monitor.tick([sample(50, frame)], now=120)
    monitor.tick([sample(60, frame)], now=125)
    assert len(sent) == 1
    newer = retry(retry()) + CHROME
    monitor.tick([sample(70, newer)], now=130)
    monitor.tick([sample(80, newer)], now=135)
    assert len(sent) == 2


@pytest.mark.parametrize("ambiguous", [False, True])
def test_no_overlap_or_ambiguous_repeated_history_fails_closed(tmp_path, ambiguous):
    monitor, sent, _ = start(tmp_path / "state.json")
    unrelated = "› Different task\n\n• Different output\n\n" + ERROR
    frame = (HISTORY + "\n\n" + HISTORY + "\n\n" + ERROR + CHROME) if ambiguous else (retry(unrelated) + CHROME)
    monitor.tick([sample(30, frame)], now=110)
    monitor.tick([sample(40, frame)], now=115)
    assert len(sent) == 1


def test_hashes_only_no_prompt_or_terminal_output_in_journal(tmp_path):
    path = tmp_path / "state.json"
    monitor, _, _ = start(path)
    secret = "private fixture instruction 721"
    monitor.tick([sample(30, retry(prompt=secret) + CHROME)], now=110)
    payload = path.read_text()
    assert secret not in payload
    assert "fixture_module.py" not in payload
    row = next(iter(json.loads(payload)["events"].values()))
    assert row.get("history")  # the boundary must actually be durable


def test_sampler_supplies_history_for_retry_without_another_capture(monkeypatch, tmp_path):
    from dashboard import server
    path = tmp_path / "state.json"
    monitor, sent, _ = start(path)
    frame = retry() + CHROME
    captures = []
    def capture(args, **kwargs):
        captures.append(args)
        return SimpleNamespace(returncode=0, stdout=frame)
    monkeypatch.setattr(server, "_tmux_run", capture)
    monkeypatch.setattr(server, "_rt_cache", {})
    monkeypatch.setattr(server, "_provider_captures", {})
    for capture_id in (30, 40):
        monkeypatch.setattr(server.time, "monotonic", lambda: capture_id)
        server._agent_runtime("ChildCurie", 101, "$1")
        row = sample(capture_id)
        row["evidence"] = server._provider_captures["ChildCurie"][2]
        monitor.tick([row], now=100 + capture_id)
    assert len(captures) == 2
    assert len(sent) == 2


def fold(history, width):
    return "\n\n".join(textwrap.fill(" ".join(block.split()), width=width,
        subsequent_indent="  ") for block in history.split("\n\n"))


@pytest.mark.parametrize("width", [60, 80, 100, 120])
@pytest.mark.parametrize("remote", [False, True])
def test_resize_and_hard_wrap_are_not_a_retry_but_submitted_input_is(tmp_path, width, remote):
    monitor, sent, _ = start(tmp_path / "state.json")
    # Repainting identical history at a different width cannot create a send.
    repainted = "\x1b[31m" + fold(HISTORY, width) + "\x1b[0m" + CHROME
    monitor.tick([sample(30, repainted)], now=110)
    monitor.tick([sample(40, repainted)], now=115)
    assert len(sent) == 1
    history = retry(prompt="Continue the fixture task and verify the saved result before reporting the outcome")
    if remote:
        prefix, last = history.rsplit(ERROR, 1)
        history = prefix + "■ Error running remote compact task: " + ERROR.removeprefix("■ ") + last
    frame = fold(history, width) + CHROME
    monitor.tick([sample(50, frame)], now=120)
    monitor.tick([sample(60, frame)], now=125)
    assert len(sent) == 2


def test_old_journal_bootstraps_without_repeating_existing_notice(tmp_path):
    path = tmp_path / "state.json"
    monitor, sent, _ = start(path)
    saved = json.loads(path.read_text())
    next(iter(saved["events"].values())).pop("history", None)
    path.write_text(json.dumps(saved))
    # First observation after an upgrade is only a baseline, even if new input
    # was missed during the outage; a later proven retry remains observable.
    frame = retry() + CHROME
    monitor.tick([sample(30, frame)], now=110)
    monitor.tick([sample(40, frame)], now=115)
    assert len(sent) == 1
    next_frame = retry(retry()) + CHROME
    monitor.tick([sample(50, next_frame)], now=120)
    monitor.tick([sample(60, next_frame)], now=125)
    assert len(sent) == 2


def test_error_alone_cannot_anchor_a_retry(tmp_path):
    path = tmp_path / "state.json"
    sent = []
    monitor = stalls.Monitor(path, lambda row, event: sent.append(event) or "sent")
    monitor.tick([sample(10, ERROR + CHROME)], now=100)
    monitor.tick([sample(20, ERROR + CHROME)], now=105)
    for capture in (30, 40):
        monitor.tick([sample(capture, retry(ERROR) + CHROME)], now=100 + capture)
    assert len(sent) == 1


def test_two_fast_retries_require_confirmation_of_latest_boundary(tmp_path):
    monitor, sent, _ = start(tmp_path / "state.json")
    frame = retry() + CHROME
    monitor.tick([sample(30, frame)], now=110)
    later = retry(retry()) + CHROME
    monitor.tick([sample(40, later)], now=115)
    assert len(sent) == 1
    monitor.tick([sample(50, later)], now=120)
    assert len(sent) == 2


def test_two_processes_claim_the_retry_only_once(tmp_path):
    path, notices = tmp_path / "state.json", tmp_path / "notices.jsonl"
    _, _, _ = start(path)
    # Both processes race the same first and second retry captures against one
    # real flock/state journal. The sender is a local file, never ORRERY Mail.
    rows = [sample(30, retry() + CHROME), sample(40, retry() + CHROME)]
    code = """import json,sys
from pathlib import Path
from dashboard.provider_stalls import Monitor
state, notices, rows = sys.argv[1:]
def send(row, event):
    with open(notices, 'a') as target:
        target.write(event['event_id']+'\\n')
    return 'sent'
m=Monitor(Path(state),send)
for row in json.loads(rows):m.tick([row],now=100+row['capture_id'])
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    workers = []
    try:
        for _ in range(2):
            workers.append(subprocess.Popen([sys.executable, "-c", code,
                str(path), str(notices), json.dumps(rows)], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        for worker in workers:
            out, err = worker.communicate(timeout=10)
            assert worker.returncode == 0, out + err
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=5)
    assert notices.is_file(), "retry notice was lost"
    assert len(notices.read_text().splitlines()) == 1


def test_pending_retry_is_not_confirmed_by_a_different_capture_boundary(tmp_path):
    monitor, sent, _ = start(tmp_path / "state.json")
    monitor.tick([sample(30, retry() + CHROME)], now=110)
    # A redraw of an earlier history tail with identical prompt words is not
    # the same input occurrence as the pending boundary.
    monitor.tick([sample(40)], now=115)
    monitor.tick([sample(50, retry() + CHROME)], now=120)
    assert len(sent) == 1


@pytest.mark.parametrize("width", [60, 100])
def test_redacted_stop_and_retry_in_owned_tmux_pane(tmp_path, width):
    """Replay saved/redacted stop chrome plus a synthetic retry, without input.

    The writer only prints fixture frames. No agent, default tmux socket, Mail
    transport, or live pane is used. The separate sampler unit checks sharing.
    """
    if not shutil.which("tmux"):
        pytest.skip("tmux unavailable")
    frame_path = tmp_path / "frame.txt"
    frame_path.write_text(fold(HISTORY, width) + CHROME)
    writer = """import pathlib,sys,time
p=pathlib.Path(sys.argv[1]); old=None
while True:
    text=p.read_text()
    if text!=old:
        print('\\x1b[2J\\x1b[3J\\x1b[H'+text,flush=True);old=text
    time.sleep(.02)
"""
    env = {**os.environ, "TMUX": ""}
    with tempfile.TemporaryDirectory(prefix="stall-retry-", dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as directory:
        socket = directory + "/s"
        def tmux(*args):
            return subprocess.run(["tmux", "-f", "/dev/null", "-S", socket, *args],
                env=env, capture_output=True, text=True, timeout=5, check=True)
        try:
            tmux("new-session", "-d", "-s", "FixtureRetry", "-x", str(width), "-y", "40",
                shlex.join([sys.executable, "-c", writer, str(frame_path)]))
            def capture(expected):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    text = tmux("capture-pane", "-p", "-J", "-t", "FixtureRetry", "-S", "-45").stdout
                    if text.count("Selected model") == expected and "sol medium" in text:
                        return text
                    time.sleep(.02)
                pytest.fail("fixture pane did not paint: " + repr(text))
            first = capture(1)
            monitor, sent, _ = start(tmp_path / "state.json")
            # Use native-captured history as the initial anchor; unchanged
            # redraw must agree with the saved redacted frame's boundary.
            monitor.tick([sample(30, first)], now=110)
            monitor.tick([sample(40, first)], now=115)
            assert len(sent) == 1
            frame_path.write_text(fold(retry(), width) + CHROME)
            second = capture(2)
            monitor.tick([sample(50, second)], now=120)
            assert len(sent) == 1
            monitor.tick([sample(60, second)], now=125)
            assert len(sent) == 2
        finally:
            subprocess.run(["tmux", "-S", socket, "kill-server"], env=env,
                capture_output=True, timeout=5)
