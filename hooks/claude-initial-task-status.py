#!/usr/bin/env python3
"""Did a Claude child act on the task it was launched with, and report it?

spawn_child.sh gives a Claude child its first prompt and then watches for the
answer, because a child can receive the prompt and still not act on it: on
2026-10-01 Sonnet 5 children outside the vault answered the launch prompt with
a question to the user instead of the task, and nothing told the parent.

The answer is read from Claude Code's own transcript, never from the screen and
never from what the model says about itself. A launched child is asked to
report to its parent with ORRERY Mail's send_message, so its first turn should
end with that report. The first turn is classified as:

  reported              it ended after a send_message to the parent that
                        ORRERY Mail accepted (its tool result is not an error)
  report_failed         it ended after a send_message to the parent that
                        failed, or whose result never came back
  declined              it ended in text alone (refused, or asked to confirm)
  ended_without_report  it called tools but ended without reporting (checked
                        and then declined, or forgot to report)
  working               it is calling tools and has not ended yet
  thinking              it has started answering, no tool yet, not ended
  pending               nothing from the child yet

Two commands:

  claude-initial-task-status.py PROJECTS_DIR SINCE_EPOCH PROMPT_HEAD [PARENT]
      one check; prints {"status", "transcript", "text", "tools"} (the canary)
  claude-initial-task-status.py watch CHILD PARENT PROMPT_HEAD [LAUNCHED_EPOCH]
      watch until the first turn ends and tell the parent by Mail when it did
      not start or did not report (the launcher runs this in the background)

An empty PROJECTS_DIR means Claude Code's own: $CLAUDE_CONFIG_DIR/projects,
else ~/.claude/projects. Never raises: anything unreadable is "pending".
"""
from __future__ import annotations

import http.client
import json
import runpy
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# A turn ends with any of these; "tool_use" means it continues with a tool.
_TURN_ENDS = {"end_turn", "stop_sequence", "max_tokens", "refusal"}
_TEXT_LIMIT = 2000
# Enough of the launch prompt's first line to name the child (it carries the
# child's name) without depending on the rest of the wording.
_HEAD_CHARS = 80
_MAIL_TOOL_SUFFIX = "orrery-mail__send_message"


def _projects_dir(raw: str) -> Path:
    if raw:
        return Path(raw)
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return ""


def _is_human_turn(row: dict) -> bool:
    """A user row the person (or launcher) wrote, not a tool result."""
    if row.get("type") != "user" or row.get("isMeta"):
        return False
    origin = row.get("origin")
    if isinstance(origin, dict) and origin.get("kind"):
        return origin.get("kind") == "human"
    content = (row.get("message") or {}).get("content")
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return isinstance(content, str)


class FirstTurn:
    """The first turn after the launch prompt, fed one transcript row at a time."""

    def __init__(self, prompt_head: str, parent: str = "") -> None:
        self.head = prompt_head.strip()[:_HEAD_CHARS]
        self.parent = parent
        self.matched: bool | None = None  # None until the first human row is seen
        self.ended = False
        self.tools: list[str] = []
        self.report_ids: set[str] = set()  # send_message calls to the parent
        self.reported = False  # one of them succeeded
        self.texts: list[str] = []
        self.assistant_rows = 0
        self.end_id: object = None  # the message that ended the turn

    def _collect(self, message: dict) -> None:
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = str(block.get("name", ""))
                self.tools.append(name)
                if name.endswith(_MAIL_TOOL_SUFFIX):
                    # Only a message addressed to the parent is the report.
                    recipients = (block.get("input") or {}).get("to")
                    if isinstance(recipients, list) and (not self.parent or self.parent in recipients):
                        self.report_ids.add(str(block.get("id", "")))
        text = _content_text(content)
        if text:
            self.texts.append(text)

    def feed(self, row: dict) -> None:
        if self.matched is False:
            return
        if self.ended:
            # Claude Code writes each block of a message as its own row; the
            # rest of the message that ended the turn still belongs to it.
            message = row.get("message")
            if (row.get("type") == "assistant" and isinstance(message, dict)
                    and self.end_id is not None and message.get("id") == self.end_id):
                self._collect(message)
            return
        if self.matched is None:
            if _is_human_turn(row):
                text = _content_text((row.get("message") or {}).get("content"))
                self.matched = bool(self.head) and self.head in text
            return
        if _is_human_turn(row):
            self.ended = True  # someone answered: the first turn is over
            return
        message = row.get("message")
        if row.get("type") == "user" and isinstance(message, dict):
            # A tool result: the report counts once ORRERY Mail accepted it.
            for block in message.get("content") if isinstance(message.get("content"), list) else []:
                if (isinstance(block, dict) and block.get("type") == "tool_result"
                        and str(block.get("tool_use_id", "")) in self.report_ids
                        and not block.get("is_error")):
                    self.reported = True
            return
        if row.get("type") != "assistant" or not isinstance(message, dict):
            return
        self.assistant_rows += 1
        self._collect(message)
        # Every row of a message carries the message's stop_reason; only the
        # turn's last message stops with anything but tool_use.
        if message.get("stop_reason") in _TURN_ENDS:
            self.ended = True
            self.end_id = message.get("id")

    def status(self) -> str:
        if not self.matched:
            return "pending"
        if self.ended:
            if self.reported:
                return "reported"
            if self.report_ids:
                return "report_failed"
            return "ended_without_report" if self.tools else "declined"
        if self.tools:
            return "working"
        return "thinking" if self.assistant_rows else "pending"

    def text(self) -> str:
        return "\n".join(part for part in self.texts if part).strip()[-_TEXT_LIMIT:]


class Transcript:
    """Follow one transcript file, reading only what was appended."""

    def __init__(self, path: Path, turn: FirstTurn) -> None:
        self.path = path
        self.turn = turn
        self.offset = 0
        self.partial = b""

    def update(self) -> None:
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
        except OSError:
            return
        self.offset += len(chunk)
        lines = (self.partial + chunk).split(b"\n")
        self.partial = lines.pop()  # an unfinished last line waits for the rest
        for line in lines:
            try:
                row = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(row, dict):
                self.turn.feed(row)
            if self.turn.ended and self.turn.end_id is None:
                return


def _epoch(stamp: object) -> float | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _launch_prompt_matches(path: Path, head: str, since: float = 0.0) -> bool:
    """Read a candidate only up to its first human row, near the top.

    A child relaunched under the same name sends the same first words, and an
    older conversation of that name can still be written to (a resume): its
    mtime says nothing about when it began. So the launch prompt itself must
    have been written after this launch started.
    """
    try:
        with path.open("rb") as handle:
            for raw in handle:
                try:
                    row = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if isinstance(row, dict) and _is_human_turn(row):
                    if head not in _content_text((row.get("message") or {}).get("content")):
                        return False
                    if since <= 0:
                        return True
                    written = _epoch(row.get("timestamp"))
                    return written is not None and written >= since - 5
    except OSError:
        return False
    return False


def find_transcript(projects: Path, since: float, prompt_head: str) -> Path | None:
    head = prompt_head.strip()[:_HEAD_CHARS]
    if not head:
        return None
    try:
        candidates = [path for path in projects.glob("*/*.jsonl") if path.stat().st_mtime >= since - 5]
    except OSError:
        return None
    for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
        if _launch_prompt_matches(path, head, since):
            return path
    return None


def check(projects: Path, since: float, prompt_head: str, parent: str = "") -> dict:
    turn = FirstTurn(prompt_head, parent)
    path = find_transcript(projects, since, prompt_head)
    if path is not None:
        Transcript(path, turn).update()
    return {"status": turn.status(), "transcript": str(path or ""), "text": turn.text(),
            "tools": turn.tools}


# --- the launcher's watcher -------------------------------------------------


def _note(message: str) -> None:
    log = os.environ.get("AGENTSTACK_SPAWN_INCIDENT_LOG") or os.path.join(
        os.environ.get("AGENTSTACK_RUNTIME_DIR") or os.path.expanduser("~/.agentstack/runtime"),
        "spawn_incidents.log")
    try:
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + " " + message + "\n")
    except OSError:
        pass


def _quote(text: str) -> str:
    return "\n".join("> " + line for line in text.splitlines())


def notice(kind: str, child: str, parent: str, wait: int, text: str) -> tuple[str, str]:
    """(subject, body) of a launcher notice. Never presented as the child's words."""
    first = f"この Mail は {child} 本人ではなく、ORRERY の launcher（spawn_child.sh）が自動で送りました。\n\n"
    look = f"tmux session `{child}` を開いて様子を見るか、タスクを渡し直してください。\n"
    notices = {
        "declined": (
            f"[launcher] {child} がタスクを始めていません",
            f"{child} は、起動時に渡したタスクを始めていません。最初の turn で tool を呼ばずに、"
            "文章だけで返しました（タスクを断ったか、確認・入力を求めています）。\n\n" + look),
        "ended_without_report": (
            f"[launcher] {child} が報告せずに最初の turn を終えました",
            f"{child} は tool を使いましたが、{parent} への ORRERY Mail の send_message を送らずに"
            "最初の turn を終え、入力を待っています。確かめた後でタスクを断ったか、報告を忘れた"
            "可能性があります。\n\n" + look),
        "report_failed": (
            f"[launcher] {child} の報告が {parent} に届いていません",
            f"{child} は {parent} への ORRERY Mail の send_message を試みましたが、成功した記録が無いまま"
            "最初の turn を終え、入力を待っています（送信が失敗したか、承認されませんでした）。\n\n" + look),
        "timeout": (
            f"[launcher] {child} がタスクを始めていません",
            f"起動から {wait} 秒の間に、{child} の最初の応答がありませんでした。始めたら、もう 1 通"
            "知らせます。\n\n" + look),
        "thinking": (
            f"[launcher] {child} はまだ最初の応答の途中です",
            f"起動から {wait} 秒たっても、{child} の最初の turn は終わらず、tool も呼んでいません"
            "（考えている途中かもしれません）。始めたら、もう 1 通知らせます。\n"),
        "unverified": (
            f"[launcher] {child} がタスクを始めたか確かめられません",
            f"起動から {wait} 秒の間に、{child} の会話の記録（Claude Code の transcript）が見つからず、"
            "始めたかを確かめられませんでした。\n\n" + look),
        "started": (
            f"[launcher] {child} がタスクを始めました（先の知らせの訂正）",
            f"先に知らせた後で、{child} は tool を呼んでタスクを始めました。\n"),
    }
    subject, body = notices[kind]
    if text and kind in {"declined", "ended_without_report", "report_failed", "thinking"}:
        body += "\n## 子の最後の発言\n\n" + _quote(text) + "\n"
    return subject, first + body


def send_notice(kind: str, child: str, parent: str, wait: int, text: str) -> str:
    """Send as the child, with its token. Returns "" or why it could not."""
    helper=Path(__file__).resolve().parents[1]/'bin/lib/runtime_client.py'
    implicit=helper.parents[2]/'runtime-client.json'
    if os.environ.get('AGENTSTACK_CLIENT_CONFIG') or implicit.exists() or implicit.is_symlink():
        try:
            api=runpy.run_path(str(helper));client=api['configured']()
            state=api['read_json'](client.outputs['child'])
            subject,body=notice(kind,child,parent,wait,text)
            client.call('send_message',{'to_agent_ids':[state['parent']['agent_id']],
                'subject':subject,'body_md':body,'importance':'normal' if kind in ('started','thinking') else 'high'})
            return ''
        except (RuntimeError,ValueError,OSError,KeyError):
            return 'global report pending or unavailable; use replay-pending'
    token_file = os.environ.get("CHILD_START_TOKEN_FILE", "")
    url = os.environ.get("CHILD_START_MCP_URL", "")
    project_key = os.environ.get("CHILD_START_PROJECT_KEY", "")
    if not parent:
        return "no parent to tell"
    try:
        with open(token_file, encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError:
        return f"the child's token is not available ({token_file or 'unset'})"
    if not token:
        return "the child's token is empty"
    subject, body = notice(kind, child, parent, wait, text)
    arguments = {"project_key": project_key, "sender_name": child, "to": [parent],
                 "subject": subject, "body_md": body, "sender_token": token,
                 "importance": "normal" if kind in {"started", "thinking"} else "high"}
    parsed = urlparse(url)
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "send_message", "arguments": arguments}}).encode()
    try:
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=30)
        connection.request("POST", parsed.path or "/", body=request,
                           headers={"Content-Type": "application/json",
                                    "Accept": "application/json", "Connection": "close"})
        reply = json.loads(connection.getresponse().read().decode("utf-8"))
        connection.close()
    except (OSError, ValueError) as exc:
        return f"ORRERY Mail at {url} did not answer: {type(exc).__name__}"
    result = reply.get("result") if isinstance(reply, dict) else None
    if not isinstance(result, dict) or reply.get("error") or result.get("isError"):
        return "ORRERY Mail refused the message"
    return ""


def _session_alive(child: str) -> bool:
    try:
        return subprocess.run(["tmux", "has-session", "-t", f"={child}"],
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True  # cannot tell: keep watching until the time limit


def watch(child: str, parent: str, prompt_head: str, launched: float | None = None) -> int:
    wait = int(os.environ.get("CHILD_START_WAIT_SECONDS") or 180)
    poll = float(os.environ.get("CHILD_START_POLL_SECONDS") or 5)
    limit = float(os.environ.get("CHILD_START_WATCH_MAX_SECONDS") or 6 * 3600)
    projects = _projects_dir("")
    # When the launcher started the child (just before tmux, or the paste into
    # a warm session), not when this watcher started: that is after the ready
    # wait and registration, and a quick child may have answered by then.
    started_at = launched if launched else time.time()
    turn = FirstTurn(prompt_head, parent)
    transcript: Transcript | None = None
    early = ""  # the early notice already sent: timeout, thinking or unverified

    def tell(kind: str) -> None:
        why = send_notice(kind, child, parent, wait, turn.text())
        told = f"told {parent} by Mail" if not why else f"could not tell {parent or 'the parent'} by Mail: {why}"
        _note(f"Claude child {child}: {kind}; {told}")

    while True:
        if transcript is None:
            path = find_transcript(projects, started_at, prompt_head)
            if path is not None:
                transcript = Transcript(path, turn)  # remembered from now on
        if transcript is not None:
            transcript.update()
        status = turn.status()
        if early and status in {"working", "reported", "ended_without_report", "report_failed"}:
            tell("started")  # correct the early notice, once
            early = ""
        if status == "reported":
            _note(f"Claude child {child} started its task and reported to {parent or 'its parent'}")
            return 0
        if status in {"declined", "ended_without_report", "report_failed"}:
            # The rest of the last message may be a moment behind its first row.
            time.sleep(min(poll, 1.0))
            transcript.update()
            tell(turn.status())
            return 0
        elapsed = time.time() - started_at
        if elapsed >= wait and not early and status != "working":
            early = "unverified" if transcript is None else ("thinking" if status == "thinking" else "timeout")
            tell(early)
        if not _session_alive(child):
            _note(f"Claude child {child} ended before its first turn finished ({status})")
            return 0
        if elapsed >= limit:
            _note(f"Claude child {child}: stopped watching after {int(limit)}s ({status})")
            return 0
        time.sleep(poll)


def main(argv: list[str]) -> int:
    if len(argv) in (5, 6) and argv[1] == "watch":
        launched = None
        if len(argv) == 6:
            try:
                launched = float(argv[5])
            except ValueError:
                launched = None
        return watch(argv[2], argv[3], argv[4], launched)
    if len(argv) in (4, 5):
        try:
            since = float(argv[2])
        except ValueError:
            since = 0.0
        parent = argv[4] if len(argv) == 5 else ""
        print(json.dumps(check(_projects_dir(argv[1]), since, argv[3], parent), ensure_ascii=False))
        return 0
    print("usage: claude-initial-task-status.py PROJECTS_DIR SINCE_EPOCH PROMPT_HEAD [PARENT]\n"
          "       claude-initial-task-status.py watch CHILD PARENT PROMPT_HEAD [LAUNCHED_EPOCH]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - a watcher must never crash the launcher
        if len(sys.argv) < 2 or sys.argv[1] != "watch":
            print(json.dumps({"status": "pending", "transcript": "", "text": "", "tools": []}))
        raise SystemExit(0)
