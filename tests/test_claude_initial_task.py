"""A Claude child gets its first prompt as an argument and is watched until it acts.

On 2026-10-01 Sonnet 5 children started outside the vault answered the pasted
launch prompt with a question to the user, 6 times in 6, however the prompt was
worded: Claude Code follows instructions inside a paste only when the user's own
message asks it to. The launcher now passes the prompt as `claude [prompt]`, and
a watcher reads the child's transcript: if the first turn calls no tool, or
nothing happens in time, the parent is told by Mail instead of waiting in silence.
"""
from __future__ import annotations

import http.server
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

import pytest

from test_spawn_child_embed_task import _fake_launch_env


ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / "hooks" / "claude-initial-task-status.py"
SPAWN = ROOT / "hooks" / "spawn_child.sh"
HEAD = "Child agent startup. AGENT_NAME=Watched; parent=ParentAgent. Follow the child-agent startup"


def _transcript(path: pathlib.Path, *assistant_rows: dict, first_user: str = HEAD + " procedure") -> None:
    rows = [
        {"type": "mode"},
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<local-command>"}},
        {"type": "user", "message": {"role": "user", "content": first_user}},
        *assistant_rows,
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _assistant(*blocks: dict, stop: str | None, message_id: str | None = None) -> dict:
    message = {"role": "assistant", "content": list(blocks), "stop_reason": stop}
    if message_id:
        message["id"] = message_id
    return {"type": "assistant", "message": message}


THINKING = {"type": "thinking", "thinking": "..."}
TOOL = {"type": "tool_use", "name": "Bash", "input": {"command": "ps"}}
QUESTION = {"type": "text", "text": "Before I run anything: did you set this up?"}
REPORT = {"type": "tool_use", "name": "mcp__orrery-mail__send_message",
          "input": {"to": ["ParentAgent"], "subject": "done", "body_md": "done"}}
DONE = {"type": "text", "text": "Reported to ParentAgent."}
# A tool result comes back as a user row; it is not someone answering.
RESULT = {"type": "user", "message": {"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}}


def _status(projects: pathlib.Path, head: str = HEAD, since: float = 0) -> dict:
    result = subprocess.run(
        [sys.executable, str(HELPER), str(projects), str(since), head, "ParentAgent"],
        text=True, capture_output=True, check=True,
    )
    return json.loads(result.stdout)


# --- reading the transcript ---------------------------------------------------


def test_a_first_turn_calling_tools_is_working_until_it_ends(tmp_path):
    _transcript(tmp_path / "p" / "a.jsonl", _assistant(THINKING, stop="tool_use"),
                _assistant(TOOL, stop="tool_use"), RESULT)
    assert _status(tmp_path)["status"] == "working"


def test_a_first_turn_that_ends_with_the_report_is_reported(tmp_path):
    _transcript(tmp_path / "p" / "a.jsonl", _assistant(TOOL, stop="tool_use"), RESULT,
                _assistant(REPORT, stop="tool_use"), RESULT, _assistant(DONE, stop="end_turn"))
    assert _status(tmp_path)["status"] == "reported"


def test_checking_and_then_declining_in_words_is_caught(tmp_path):
    """Review of #163, P2-2: tools first, then a refusal, and no report."""
    check = {"type": "tool_use", "name": "mcp__orrery-mail__runtime_status", "input": {}}
    _transcript(tmp_path / "p" / "a.jsonl", _assistant(check, stop="tool_use"), RESULT,
                _assistant(QUESTION, stop="end_turn"))
    status = _status(tmp_path)
    assert status["status"] == "ended_without_report"
    assert "did you set this up?" in status["text"]


def test_a_report_to_someone_else_is_not_the_report(tmp_path):
    other = dict(REPORT, input={"to": ["SomeoneElse"], "subject": "x", "body_md": "x"})
    _transcript(tmp_path / "p" / "a.jsonl", _assistant(other, stop="tool_use"), RESULT,
                _assistant(DONE, stop="end_turn"))
    assert _status(tmp_path)["status"] == "ended_without_report"


def test_a_first_turn_still_being_written_is_thinking(tmp_path):
    _transcript(tmp_path / "p" / "a.jsonl", _assistant(THINKING, stop=None))
    assert _status(tmp_path)["status"] == "thinking"


def test_a_first_turn_of_text_alone_is_declined_with_its_words(tmp_path):
    """The shape every refusal had on 2026-10-01: thinking, text, end_turn."""
    # Thinking and text are two rows of one message, both stamped end_turn.
    _transcript(tmp_path / "p" / "a.jsonl", _assistant(THINKING, stop="end_turn", message_id="m1"),
                _assistant(QUESTION, stop="end_turn", message_id="m1"))
    status = _status(tmp_path)
    assert status["status"] == "declined"
    assert "did you set this up?" in status["text"]


def test_nothing_from_the_child_yet_is_pending(tmp_path):
    _transcript(tmp_path / "p" / "a.jsonl")
    assert _status(tmp_path)["status"] == "pending"


def test_another_childs_transcript_is_not_read_as_this_one(tmp_path):
    _transcript(tmp_path / "p" / "other.jsonl", _assistant(QUESTION, stop="end_turn"),
                first_user="Child agent startup. AGENT_NAME=Other; parent=ParentAgent.")
    assert _status(tmp_path) == {"status": "pending", "transcript": "", "text": "", "tools": []}


def test_a_transcript_older_than_the_launch_is_ignored(tmp_path):
    path = tmp_path / "p" / "a.jsonl"
    _transcript(path, _assistant(QUESTION, stop="end_turn"))
    os.utime(path, (1000, 1000))
    assert _status(tmp_path, since=time.time())["status"] == "pending"


def test_a_long_multibyte_first_line_still_matches(tmp_path):
    """The head is cut by characters; cutting bytes split a character."""
    first = "あなたは 子（親: 親）。この起動は --embed-task mode です。" + "日本語の文" * 40
    _transcript(tmp_path / "p" / "a.jsonl", _assistant(TOOL, stop="tool_use"), first_user=first)
    assert _status(tmp_path, head=first)["status"] == "working"


# --- the launcher -------------------------------------------------------------


class _Mail(http.server.BaseHTTPRequestHandler):
    calls: list[dict] = []

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        _Mail.calls.append(json.loads(self.rfile.read(length)))
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "result": {"content": [{"type": "text", "text": "{}"}]}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: D401 - silence
        pass


@pytest.fixture
def mail():
    _Mail.calls = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Mail)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/mcp"
    finally:
        server.shutdown()


def _spawn_watched(tmp_path, mail_url, *, transcript_rows=None, wait="6", extra=None):
    env, workdir = _fake_launch_env(tmp_path, codex=False)
    claude_home = tmp_path / "claude-home"
    (claude_home / "projects").mkdir(parents=True)
    env.update({
        "AGENTSTACK_CHILD_START_CHECK": "1",
        "AGENTSTACK_CHILD_START_WAIT_SECONDS": wait,
        "AGENTSTACK_CHILD_START_POLL_SECONDS": "1",
        "AGENTSTACK_MCP_URL": mail_url,
        "CLAUDE_CONFIG_DIR": str(claude_home),
        "AGENTSTACK_SPAWN_INCIDENT_LOG": str(tmp_path / "incidents.log"),
        **(extra or {}),
    })
    handoff = tmp_path / "child-token"
    handoff.write_text("child-owner-token", encoding="utf-8")
    handoff.chmod(0o600)
    binding = handoff.with_name(handoff.name + ".binding.json")
    binding.write_text(json.dumps({"agent_id": 73, "agent_name": "Watched",
                                   "project_key": "/shared/project", "program": "claude-code"}),
                       encoding="utf-8")
    binding.chmod(0o600)
    result = subprocess.run(
        ["/bin/bash", str(SPAWN), "--pre-registered", "Watched",
         "--child-token-file", str(handoff), "mail task", str(workdir)],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    # The child writes its transcript once it is running, after the launch.
    if transcript_rows is not None:
        _transcript(claude_home / "projects" / "workdir" / "child.jsonl", *transcript_rows)
    return env


@pytest.fixture(autouse=True)
def _end_fake_sessions(tmp_path):
    """A watcher follows its child until the session ends; end it after a test."""
    yield
    (tmp_path / "tmux.alive").unlink(missing_ok=True)


def _append(env, *rows):
    path = pathlib.Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / "workdir" / "child.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _wait_for(predicate, seconds=20.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def _incidents(tmp_path) -> str:
    log = tmp_path / "incidents.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


def test_a_child_that_declines_is_reported_to_its_parent_by_the_launcher(tmp_path, mail):
    _spawn_watched(tmp_path, mail, transcript_rows=[
        _assistant(THINKING, stop="end_turn", message_id="m1"),
                _assistant(QUESTION, stop="end_turn", message_id="m1")])
    assert _wait_for(lambda: _Mail.calls), _incidents(tmp_path)
    arguments = _Mail.calls[0]["params"]["arguments"]
    assert _Mail.calls[0]["params"]["name"] == "send_message"
    assert arguments["sender_name"] == "Watched" and arguments["to"] == ["ParentAgent"]
    assert arguments["sender_token"] == "child-owner-token"
    assert arguments["subject"] == "[launcher] Watched がタスクを始めていません"
    # Never presented as the child's own words.
    assert arguments["body_md"].startswith(
        "この Mail は Watched 本人ではなく、ORRERY の launcher（spawn_child.sh）が自動で送りました。")
    assert "> Before I run anything: did you set this up?" in arguments["body_md"]
    assert _wait_for(lambda: "Watched: declined; told ParentAgent by Mail" in _incidents(tmp_path))


def test_a_child_that_works_and_reports_is_left_alone(tmp_path, mail):
    env = _spawn_watched(tmp_path, mail, transcript_rows=[
        _assistant(THINKING, stop="tool_use"), _assistant(TOOL, stop="tool_use"), RESULT])
    time.sleep(2)
    _append(env, _assistant(REPORT, stop="tool_use"), RESULT, _assistant(DONE, stop="end_turn"))
    assert _wait_for(lambda: "Watched started its task and reported" in _incidents(tmp_path))
    assert _Mail.calls == []


def test_a_child_that_ends_without_reporting_is_reported_by_the_launcher(tmp_path, mail):
    check = {"type": "tool_use", "name": "mcp__orrery-mail__runtime_status", "input": {}}
    _spawn_watched(tmp_path, mail, transcript_rows=[
        _assistant(check, stop="tool_use"), RESULT, _assistant(QUESTION, stop="end_turn")])
    assert _wait_for(lambda: _Mail.calls), _incidents(tmp_path)
    arguments = _Mail.calls[0]["params"]["arguments"]
    assert arguments["subject"] == "[launcher] Watched が報告せずに最初の turn を終えました"
    assert "> Before I run anything: did you set this up?" in arguments["body_md"]


def test_a_child_still_thinking_at_the_deadline_is_told_apart_and_corrected(tmp_path, mail):
    """Review of #163, P2-3: no "not started" for a long first turn, and a
    correction once the child does start."""
    env = _spawn_watched(tmp_path, mail, wait="2", transcript_rows=[_assistant(THINKING, stop=None)])
    assert _wait_for(lambda: _Mail.calls), _incidents(tmp_path)
    first = _Mail.calls[0]["params"]["arguments"]
    assert first["subject"] == "[launcher] Watched はまだ最初の応答の途中です"
    assert first["importance"] == "normal"
    _append(env, _assistant(TOOL, stop="tool_use"))
    assert _wait_for(lambda: len(_Mail.calls) >= 2), _incidents(tmp_path)
    assert _Mail.calls[1]["params"]["arguments"]["subject"] == (
        "[launcher] Watched がタスクを始めました（先の知らせの訂正）")


def test_no_transcript_at_all_is_reported_as_unverified_not_as_declined(tmp_path, mail):
    _spawn_watched(tmp_path, mail, transcript_rows=None)
    assert _wait_for(lambda: _Mail.calls), _incidents(tmp_path)
    arguments = _Mail.calls[0]["params"]["arguments"]
    assert arguments["subject"] == "[launcher] Watched がタスクを始めたか確かめられません"
    assert "確かめられませんでした" in arguments["body_md"]


def test_the_check_is_off_when_the_suite_says_so(tmp_path, mail):
    env, workdir = _fake_launch_env(tmp_path, codex=False)
    assert env.get("AGENTSTACK_CHILD_START_CHECK") == "0"


def test_the_wait_defaults_to_three_minutes():
    text = SPAWN.read_text(encoding="utf-8")
    assert 'CLAUDE_START_WAIT_SECONDS="${AGENTSTACK_CHILD_START_WAIT_SECONDS:-180}"' in text


def test_the_prompt_names_the_orrery_mail_tool():
    """Haiku 4.5 and Sonnet 5 children reported through Claude Code's SendMessage."""
    text = SPAWN.read_text(encoding="utf-8")
    start = text.index("build_embedded_task_prompt() {")
    builder = text[start:text.index("\n}\n", start)]
    assert "ORRERY Mail の send_message（Claude Code の SendMessage ではない）" in builder


def test_the_launch_says_who_started_the_session_in_the_system_prompt(tmp_path):
    """An identity claimed inside the conversation read as an injection.

    With the task as the first user message alone, Sonnet 5 children outside
    the vault still declined 2 of 3 on 2026-10-01; with the operator's launch
    command also saying, in the system prompt, who started the session and
    what its first message is, 3 of 3 started.
    """
    env, workdir = _fake_launch_env(tmp_path, codex=False)
    handoff = tmp_path / "child-token"
    handoff.write_text("child-owner-token", encoding="utf-8")
    handoff.chmod(0o600)
    handoff.with_name(handoff.name + ".binding.json").write_text(json.dumps(
        {"agent_id": 73, "agent_name": "Told", "project_key": "/shared/project",
         "program": "claude-code"}), encoding="utf-8")
    handoff.with_name(handoff.name + ".binding.json").chmod(0o600)
    result = subprocess.run(
        ["/bin/bash", str(SPAWN), "--pre-registered", "Told",
         "--child-token-file", str(handoff), "mail task", str(workdir)],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    log = pathlib.Path(env["FAKE_TMUX_LOG"]).read_text(encoding="utf-8")
    launch = next(chunk for chunk in log.split("CALL")[1:] if chunk.startswith("\034new-session"))
    args = launch.split("\035", 1)[0].split("\034")
    system = next(arg for arg in args if arg.startswith("CLAUDE_CHILD_SYSTEM_PROMPT="))
    assert "the ORRERY child agent Told" in system and "parent agent ParentAgent" in system
    assert "mcp__orrery-mail__send_message" in system
    # Facts only (review of #163, P2-1): the launcher does not know that a
    # person asked for this task, so it must not say so.
    assert "at the request" not in system and "person who runs both" not in system
    assert "using your usual judgment" in system
    assert '--append-system-prompt "$CLAUDE_CHILD_SYSTEM_PROMPT"' in args[-1]


def test_a_prompt_too_long_for_one_argument_goes_through_a_private_file(tmp_path):
    """Linux allows 128 KiB per argument; beyond it `claude` cannot start."""
    env, workdir = _fake_launch_env(tmp_path, codex=False)
    env["AGENTSTACK_CLAUDE_ARGV_PROMPT_MAX_BYTES"] = "200"
    handoff = tmp_path / "child-token"
    handoff.write_text("child-owner-token", encoding="utf-8")
    handoff.chmod(0o600)
    handoff.with_name(handoff.name + ".binding.json").write_text(json.dumps(
        {"agent_id": 73, "agent_name": "Long", "project_key": "/shared/project",
         "program": "claude-code"}), encoding="utf-8")
    handoff.with_name(handoff.name + ".binding.json").chmod(0o600)
    task = tmp_path / "task.md"
    task.write_text("Long task. " * 100, encoding="utf-8")
    result = subprocess.run(
        ["/bin/bash", str(SPAWN), "--pre-registered", "Long", "--child-token-file", str(handoff),
         "--embed-task", "--task-file", str(task), str(workdir)],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    log = pathlib.Path(env["FAKE_TMUX_LOG"]).read_text(encoding="utf-8")
    passed = log.split("ARGV_TASK\034600\034", 1)[1].split("CALL", 1)[0]
    # The argument starts as the prompt does (so it is recognised) and points
    # at the file that holds the whole prompt.
    assert passed.startswith("あなたは Long: this launch prompt is too long")
    task_file = pathlib.Path(passed.split("in the file ", 1)[1].split(". Read", 1)[0])
    assert task_file.stat().st_mode & 0o777 == 0o600
    assert "Long task. Long task." in task_file.read_text(encoding="utf-8")
