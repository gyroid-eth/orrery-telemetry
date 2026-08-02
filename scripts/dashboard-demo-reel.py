#!/usr/bin/env python3
"""Run the deterministic 25-second AgentStack concept-demo timeline.

The reel uses a newly created SQLite database, an isolated dashboard process,
and exactly three real tmux printer sessions.  All remaining panes are virtual
inside the demo-only tmux adapter.  Existing AgentStack data, port 8770, and
unrelated tmux sessions are never read or mutated.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
DOCS_DEMO_PATH = HERE / "dashboard-demo.py"
PRINTER_SOURCE = HERE / "dashboard-demo-printer.py"
DEFAULT_INSTALL_DIR = Path("/private/tmp/agentstack-demo-reel")
DEFAULT_PORT = 8878
PROFILE = "agentstack-demo-reel-v1"
SESSION_PREFIX = "agentstack-reel-"
STORY_SECONDS = 25.0


def _load_docs_demo():
    spec = importlib.util.spec_from_file_location("agentstack_dashboard_demo", DOCS_DEMO_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {DOCS_DEMO_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DEMO = _load_docs_demo()


AGENTS: dict[str, dict[str, Any]] = {
    "Bright-Curie": {
        "id": 1, "model": "claude-opus-4-7[1m]", "program": "claude-code",
        "task": "Coordinate the fictional orbital observatory launch",
        "role": "Mission lead", "emoji": "🧭", "group": "Signal Lab", "real": True,
    },
    "Swift-Noether": {
        "id": 2, "model": "gpt-5.6-sol", "program": "codex-cli",
        "task": "Coordinate fictional telemetry systems",
        "role": "Systems lead", "emoji": "🎛️", "group": "Orbit Studio", "real": True,
    },
    "Calm-Turing": {
        "id": 3, "model": "claude-sonnet-5", "program": "claude-code",
        "task": "Coordinate the fictional visitor signal map",
        "role": "Journey lead", "emoji": "🪐", "group": "Lumen Studio", "real": True,
    },
    "Warm-Lovelace": {
        "id": 4, "model": "gpt-5.6-terra", "program": "codex-cli",
        "task": "Compose fictional navigation cards",
        "role": "Navigation designer", "emoji": "🗺️", "group": "Signal Lab", "real": False,
    },
    "Bold-Hopper": {
        "id": 5, "model": "claude-sonnet-5", "program": "claude-code",
        "task": "Build fictional telemetry transforms",
        "role": "Telemetry engineer", "emoji": "📡", "group": "Orbit Studio", "real": False,
    },
    "Quiet-Franklin": {
        "id": 6, "model": "gpt-5.6-sol", "program": "codex-cli",
        "task": "Draft fictional constellation labels",
        "role": "Constellation editor", "emoji": "✨", "group": "Lumen Studio", "real": False,
    },
    "Keen-Faraday": {
        "id": 7, "model": "claude-haiku-4-5-20251001", "program": "claude-code",
        "task": "Tune fictional signal relays",
        "role": "Relay tuner", "emoji": "⚡", "group": "Signal Lab", "real": False,
    },
    "Gentle-Lamarr": {
        "id": 8, "model": "gpt-5.6-terra", "program": "codex-cli",
        "task": "Map fictional spectrum channels",
        "role": "Spectrum mapper", "emoji": "🌈", "group": "Orbit Studio", "real": False,
    },
    "Steady-Bose": {
        "id": 9, "model": "claude-opus-4-7[1m]", "program": "claude-code",
        "task": "Balance fictional ambient signals",
        "role": "Signal composer", "emoji": "🎧", "group": "Lumen Studio", "real": False,
    },
    "Soft-Galileo": {
        "id": 10, "model": "gpt-5.6-sol", "program": "codex-cli",
        "task": "Review fictional orbital scale cues",
        "role": "Scale reviewer", "emoji": "🔭", "group": "Signal Lab", "real": False,
    },
    "Clear-Somerville": {
        "id": 11, "model": "claude-sonnet-5", "program": "claude-code",
        "task": "Validate fictional observation notes",
        "role": "Observation editor", "emoji": "📝", "group": "Orbit Studio", "real": False,
    },
    "Vivid-Feynman": {
        "id": 12, "model": "gpt-5.6-terra", "program": "codex-cli",
        "task": "Connect fictional exhibit handoffs",
        "role": "Handoff designer", "emoji": "🔗", "group": "Lumen Studio", "real": False,
    },
}

REAL_AGENT_NAMES = tuple(name for name, data in AGENTS.items() if data["real"])
REAL_SESSION_BY_AGENT = {
    "Bright-Curie": f"{SESSION_PREFIX}Curie",
    "Swift-Noether": f"{SESSION_PREFIX}Noether",
    "Calm-Turing": f"{SESSION_PREFIX}Turing",
}
REAL_SESSION_NAMES = tuple(REAL_SESSION_BY_AGENT[name] for name in REAL_AGENT_NAMES)

CAPTIONS = (
    {"at": 0.0, "text": "ONE TERMINAL"},
    {"at": 3.0, "text": "THREE PROJECTS"},
    {"at": 5.0, "text": "AGENTS MULTIPLY"},
    {"at": 9.0, "text": "1 HUMAN · 12 AGENTS"},
    {"at": 10.5, "text": "AGENTS NEGOTIATING"},
    {"at": 15.0, "text": "HUMAN IN THE LOOP"},
    {"at": 21.0, "text": "ONE CLICK · SYSTEM MOVES"},
)


def _agent_add(at: float, name: str) -> dict[str, Any]:
    return {"at": at, "type": "agent_add", "agent": name}


def _spawn(at: float, parent: str, child: str) -> dict[str, Any]:
    return {"at": at, "type": "spawn", "parent": parent, "child": child}


def _mail(at: float, sender: str, recipient: str, subject: str) -> dict[str, Any]:
    return {
        "at": at, "type": "mail", "sender": sender, "recipient": recipient,
        "subject": subject,
        "body": "Fictional observatory demo data; coordinate the next visual handoff.",
    }


def _state(at: float, agent: str, act_state: str, ctx_used: int) -> dict[str, Any]:
    return {
        "at": at, "type": "state", "agent": agent,
        "act_state": act_state, "ctx_used": ctx_used,
    }


# The story itself is data.  Every event is one of the four public primitives.
TIMELINE: tuple[dict[str, Any], ...] = (
    _agent_add(0.0, "Bright-Curie"),
    _state(0.0, "Bright-Curie", "work", 12),

    _agent_add(3.0, "Swift-Noether"),
    _agent_add(3.0, "Calm-Turing"),
    _state(3.0, "Swift-Noether", "wait", 19),
    _state(3.0, "Calm-Turing", "work", 24),

    _agent_add(5.0, "Warm-Lovelace"),
    _agent_add(5.0, "Bold-Hopper"),
    _agent_add(5.0, "Quiet-Franklin"),
    _spawn(5.1, "Bright-Curie", "Warm-Lovelace"),
    _spawn(5.1, "Swift-Noether", "Bold-Hopper"),
    _spawn(5.1, "Calm-Turing", "Quiet-Franklin"),

    _agent_add(7.0, "Keen-Faraday"),
    _agent_add(7.0, "Gentle-Lamarr"),
    _agent_add(7.0, "Steady-Bose"),
    _spawn(7.1, "Bright-Curie", "Keen-Faraday"),
    _spawn(7.1, "Swift-Noether", "Gentle-Lamarr"),
    _spawn(7.1, "Calm-Turing", "Steady-Bose"),

    _agent_add(9.0, "Soft-Galileo"),
    _agent_add(9.0, "Clear-Somerville"),
    _agent_add(9.0, "Vivid-Feynman"),
    _state(9.0, "Bright-Curie", "question", 18),
    _state(9.0, "Swift-Noether", "ask", 27),
    _spawn(9.1, "Bright-Curie", "Soft-Galileo"),
    _spawn(9.1, "Swift-Noether", "Clear-Somerville"),
    _spawn(9.1, "Calm-Turing", "Vivid-Feynman"),

    _mail(10.5, "Warm-Lovelace", "Bold-Hopper", "Align navigation and telemetry"),
    _mail(10.5, "Bold-Hopper", "Quiet-Franklin", "Share the signal scale"),
    _mail(10.5, "Quiet-Franklin", "Warm-Lovelace", "Constellation labels ready"),
    _mail(11.5, "Keen-Faraday", "Gentle-Lamarr", "Relay spectrum handoff"),
    _mail(11.5, "Gentle-Lamarr", "Steady-Bose", "Spectrum cue accepted"),
    _mail(11.5, "Steady-Bose", "Keen-Faraday", "Ambient signal balanced"),
    _mail(13.0, "Soft-Galileo", "Clear-Somerville", "Review the orbital scale"),
    _mail(13.0, "Clear-Somerville", "Vivid-Feynman", "Observation notes verified"),
    _mail(13.0, "Vivid-Feynman", "Soft-Galileo", "Handoff path connected"),

    _state(15.0, "Bright-Curie", "wait", 21),
    _state(15.0, "Swift-Noether", "work", 31),
    _state(15.0, "Calm-Turing", "ask", 36),
    _mail(15.5, "Bright-Curie", "Swift-Noether", "Human decision received"),
    _mail(15.5, "Swift-Noether", "Calm-Turing", "Resume coordinated rollout"),
    _mail(16.5, "Calm-Turing", "Bright-Curie", "Rollout path confirmed"),
    _mail(17.5, "Bold-Hopper", "Warm-Lovelace", "Telemetry cards synchronized"),
    _mail(18.5, "Gentle-Lamarr", "Quiet-Franklin", "Shared spectrum token"),
    _mail(19.5, "Steady-Bose", "Clear-Somerville", "Observation rhythm aligned"),

    _state(21.0, "Swift-Noether", "question", 38),
    _state(21.0, "Calm-Turing", "wait", 41),
    _mail(21.5, "Soft-Galileo", "Vivid-Feynman", "Final scale pass"),
    _mail(22.0, "Vivid-Feynman", "Bright-Curie", "Network handoff complete"),
    _mail(22.5, "Bright-Curie", "Warm-Lovelace", "Publish navigation cue"),
    _mail(23.0, "Swift-Noether", "Bold-Hopper", "Publish telemetry cue"),
    _mail(23.5, "Calm-Turing", "Quiet-Franklin", "Publish constellation cue"),
    _mail(24.0, "Warm-Lovelace", "Calm-Turing", "All fictional cues green"),
)

EVENT_TYPES = frozenset({"agent_add", "spawn", "mail", "state"})
EXPECTED_SPAWN = {
    (event["parent"], event["child"])
    for event in TIMELINE if event["type"] == "spawn"
}
EXPECTED_MESSAGES = sum(event["type"] in {"spawn", "mail"} for event in TIMELINE)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _timeline_path(root: Path) -> Path:
    return root / "runtime" / "reel-timeline.json"


def _status_path(root: Path) -> Path:
    return root / "runtime" / "reel-status.json"


def _session_state_path(root: Path) -> Path:
    return root / "runtime" / "reel-sessions.json"


def _printer_state_path(root: Path, name: str) -> Path:
    return root / "runtime" / "printer-states" / f"{name}.json"


def _current_caption(elapsed: float) -> str:
    caption = CAPTIONS[0]["text"]
    for item in CAPTIONS:
        if elapsed < float(item["at"]):
            break
        caption = str(item["text"])
    return caption


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _real_tmux() -> str:
    candidate = shutil.which("tmux")
    if not candidate:
        raise RuntimeError("tmux is required for the three demo printers")
    path = str(Path(candidate).resolve())
    if path.startswith(str(DEFAULT_INSTALL_DIR)):
        raise RuntimeError("refusing the demo tmux adapter as the real tmux binary")
    return path


def _tmux_env() -> dict[str, str]:
    env = os.environ.copy()
    env["TMUX"] = ""
    return env


def _tmux_run(tmux_bin: str, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [tmux_bin, *args], capture_output=True, text=True, timeout=8,
        check=check, env=_tmux_env(),
    )


def _has_real_session(tmux_bin: str, name: str) -> bool:
    return _tmux_run(tmux_bin, ["has-session", "-t", f"={name}"], check=False).returncode == 0


def _kill_real_sessions(tmux_bin: str, names: tuple[str, ...] = REAL_SESSION_NAMES) -> None:
    for name in names:
        if not name.startswith(SESSION_PREFIX):
            raise RuntimeError(f"refusing non-demo tmux session name: {name}")
        if _has_real_session(tmux_bin, name):
            _tmux_run(tmux_bin, ["kill-session", "-t", f"={name}"])


def _state_capture(name: str, act_state: str, ctx_used: int) -> str:
    data = AGENTS[name]
    header = (
        f"AgentStack concept reel · {name}\n"
        f"{data['task']}\n\n"
    )
    if act_state == "work":
        activity = "• Working (12s • esc to interrupt)"
    elif act_state == "question":
        activity = (
            "Choose the next coordinated rollout\n"
            "❯ Cross-project handoff\n"
            "Enter to select · Esc to cancel"
        )
    elif act_state == "ask":
        activity = (
            "Do you want to approve the coordinated handoff?\n"
            "❯ 1. Approve and continue\n"
            "  2. Keep waiting"
        )
    elif act_state == "wait":
        activity = "Coordination checkpoint ready.\nCrunched for 14s"
    else:
        raise ValueError(f"unknown act_state: {act_state}")
    if str(data["program"]).startswith("codex"):
        status = f"gpt-5.6-sol xhigh · Context {100 - ctx_used}% left · /demo/orbit"
    else:
        family = "Opus 4.7" if "opus" in str(data["model"]) else "Sonnet 5"
        window = "1M" if "opus" in str(data["model"]) else "200K"
        status = f"| {family} | ctx: {ctx_used}% used | ({window} context)"
    return header + activity + "\n" + status + "\n"


def _default_capture(name: str) -> str:
    return _state_capture(name, "wait", 8 + int(AGENTS[name]["id"]))


def _create_database(root: Path) -> None:
    mail = root / "mail"
    mail.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(mail / "storage.sqlite3") as con:
        con.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY,
                human_key TEXT NOT NULL UNIQUE,
                slug TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                model TEXT NOT NULL,
                program TEXT NOT NULL,
                task_description TEXT,
                inception_ts TEXT NOT NULL,
                last_active_ts TEXT NOT NULL,
                retired_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                thread_id TEXT,
                topic TEXT,
                subject TEXT,
                body_md TEXT,
                importance TEXT,
                ack_required INTEGER NOT NULL DEFAULT 0,
                created_ts TEXT NOT NULL
            );
            CREATE TABLE message_recipients (
                message_id INTEGER NOT NULL,
                agent_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                read_ts TEXT,
                ack_ts TEXT,
                PRIMARY KEY (message_id, agent_id, kind)
            );
            """
        )
        now = datetime.now(timezone.utc)
        con.execute(
            "INSERT INTO projects(id, human_key, slug, created_at) VALUES (1, ?, ?, ?)",
            (str(root / "project"), "fictional-agentstack-reel", _iso(now)),
        )


def _create_runtime(root: Path) -> None:
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    _atomic_json(runtime / "annotations.json", {"agents": {}})
    _atomic_json(
        _timeline_path(root),
        {
            "version": 1,
            "duration_seconds": STORY_SECONDS,
            "captions": CAPTIONS,
            "events": TIMELINE,
        },
    )
    _atomic_json(_session_state_path(root), {"version": 0, "sessions": {}})
    _atomic_json(
        _status_path(root),
        {"profile": PROFILE, "phase": "ready", "elapsed": 0.0,
         "caption": _current_caption(0.0), "applied_events": 0},
    )


def _create_tmux_adapter(root: Path, real_tmux: str) -> None:
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    state_path = str(_session_state_path(root))
    # Use the current interpreter directly.  The dashboard's intentionally
    # minimal PATH would otherwise resolve /usr/bin/python3; starting that
    # interpreter once per capture made a 12-node /api/agents poll exceed 3s.
    adapter = f'''#!{sys.executable}
import json
import subprocess
import sys

STATE_PATH = {state_path!r}
REAL_TMUX = {real_tmux!r}
SEP = chr(31)

try:
    with open(STATE_PATH, encoding="utf-8") as handle:
        sessions = json.load(handle).get("sessions", {{}})
except (OSError, ValueError, TypeError):
    sessions = {{}}

args = sys.argv[1:]
command = args[0] if args else ""
if command == "list-sessions":
    for name, data in sessions.items():
        print(SEP.join((name, str(data.get("created", 0)), str(data.get("activity", 0)))))
elif command == "list-panes":
    for name, data in sessions.items():
        print(SEP.join((name, "11", str(data.get("cmd", "zsh")), str(data.get("title", "")))))
elif command == "list-clients":
    pass
elif command in ("capture-pane", "has-session"):
    target_index = args.index("-t") + 1 if "-t" in args else -1
    raw_target = args[target_index] if target_index >= 0 else ""
    exact = raw_target.startswith("=")
    target = raw_target.lstrip("=")
    data = sessions.get(target)
    if data is None:
        raise SystemExit(1)
    real_target = str(data.get("real_target") or "")
    if data.get("real") and real_target:
        real_args = list(args)
        real_args[target_index] = ("=" if exact else "") + real_target
        result = subprocess.run([REAL_TMUX, *real_args])
        raise SystemExit(result.returncode)
    if command == "has-session":
        raise SystemExit(0)
    print(str(data.get("capture", "")), end="")
else:
    raise SystemExit(1)
'''
    tmux = bin_dir / "tmux"
    tmux.write_text(adapter, encoding="utf-8")
    tmux.chmod(0o755)
    for command in ("launchctl", "pkill", "ttyd"):
        shim = bin_dir / command
        shim.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        shim.chmod(0o755)


def _create_reel_server(root: Path) -> Path:
    wrapper = root / "payload" / "reel_server.py"
    wrapper.write_text(
        f'''#!/usr/bin/env python3
import json
import os
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

dashboard = Path(__file__).resolve().parent / "dashboard"
sys.path.insert(0, str(dashboard))
import server

STATUS = Path({str(_status_path(root))!r})
DB = Path({str(root / "mail" / "storage.sqlite3")!r})
graph_signature = None

class ReadOnlyReelHandler(server.Handler):
    def do_GET(self):
        global graph_signature
        path = urlparse(self.path).path
        if path == "/api/demo-info":
            body = {{"kind": {DEMO.MARKER_KIND!r}, "fixture_seed": {DEMO.FIXTURE_SEED!r}, "profile": {PROFILE!r}}}
            self._send(200, json.dumps(body).encode(), "application/json; charset=utf-8")
            return
        if path == "/api/demo-reel":
            try:
                body = json.loads(STATUS.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                body = {{"profile": {PROFILE!r}, "phase": "unknown"}}
            self._send(200, json.dumps(body).encode(), "application/json; charset=utf-8")
            return
        if path == "/api/ptty":
            self._send(403, json.dumps({{"ok": False, "error": "read-only concept demo"}}).encode(), "application/json; charset=utf-8")
            return
        if path == "/api/graph":
            try:
                stat = DB.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                signature = None
            if signature != graph_signature:
                graph_signature = signature
                server._GRAPH_CACHE.update(ts=0, data=None)
        super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path == "/__demo_shutdown__":
            if self.headers.get("X-AgentStack-Demo-Token", "") != os.environ.get("AGENTSTACK_DEMO_CONTROL_TOKEN", ""):
                self._send(403, json.dumps({{"ok": False, "error": "invalid demo control token"}}).encode(), "application/json; charset=utf-8")
                return
            self._send(200, json.dumps({{"ok": True}}).encode(), "application/json; charset=utf-8")
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        self._send(403, json.dumps({{"ok": False, "error": "read-only concept demo"}}).encode(), "application/json; charset=utf-8")

def main():
    srv = server.ThreadingHTTPServer((server.BIND_HOST, server.PORT), ReadOnlyReelHandler)
    print(f"agent-dashboard reel listening on http://{{server.BIND_HOST}}:{{server.PORT}}/", flush=True)
    srv.serve_forever()
    srv.server_close()

if __name__ == "__main__":
    main()
''',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def _write_printer(root: Path, name: str, capture: str) -> None:
    _atomic_json(_printer_state_path(root, name), {"agent": name, "capture": capture})


def _start_real_sessions(root: Path, real_tmux: str) -> None:
    collisions = [name for name in REAL_SESSION_NAMES if _has_real_session(real_tmux, name)]
    if collisions:
        raise RuntimeError(f"refusing existing tmux sessions: {', '.join(collisions)}")
    printer = root / "runtime" / "dashboard-demo-printer.py"
    shutil.copy2(PRINTER_SOURCE, printer)
    printer.chmod(0o755)
    created: list[str] = []
    try:
        for name in REAL_AGENT_NAMES:
            session_name = REAL_SESSION_BY_AGENT[name]
            _write_printer(root, name, _default_capture(name))
            command = shlex.join(
                [sys.executable, "-u", str(printer),
                 "--state-file", str(_printer_state_path(root, name)), "--agent", name]
            )
            # CLAUDECODE=1 is deliberately an explicit tmux server env entry.
            _tmux_run(
                real_tmux,
                ["new-session", "-d", "-e", "CLAUDECODE=1", "-s", session_name, command],
            )
            created.append(session_name)
    except Exception:
        _kill_real_sessions(real_tmux, tuple(created))
        raise


def _annotations(root: Path) -> dict[str, Any]:
    raw = _read_json(root / "runtime" / "annotations.json", {"agents": {}})
    return raw if isinstance(raw, dict) and isinstance(raw.get("agents"), dict) else {"agents": {}}


def _sessions(root: Path) -> dict[str, Any]:
    raw = _read_json(_session_state_path(root), {"version": 0, "sessions": {}})
    return raw if isinstance(raw, dict) and isinstance(raw.get("sessions"), dict) else {"version": 0, "sessions": {}}


def _touch_session(root: Path, name: str, *, capture: str | None = None) -> None:
    payload = _sessions(root)
    sessions = payload["sessions"]
    now = int(time.time())
    data = AGENTS[name]
    current = sessions.get(name, {})
    title = "• Coordinating fictional observatory work" if str(data["program"]).startswith("codex") else "✻ Coordinating fictional observatory work"
    current.update(
        {
            "created": int(current.get("created") or now),
            "activity": now,
            "cmd": "zsh" if str(data["program"]).startswith("codex") else "claude",
            "title": title,
            "capture": capture if capture is not None else current.get("capture", _default_capture(name)),
            "real": bool(data["real"]),
            "real_target": REAL_SESSION_BY_AGENT.get(name),
        }
    )
    sessions[name] = current
    payload["version"] = int(payload.get("version") or 0) + 1
    _atomic_json(_session_state_path(root), payload)
    if data["real"]:
        _write_printer(root, name, str(current["capture"]))
        if capture is not None:
            # tmux keeps the previously visible screen in history even after
            # xterm's ESC[3J.  Work has parser priority, so a stale "esc to
            # interrupt" would mask a later question/ask.  Wait for the real
            # printer to redraw, then clear only this owned pane's history.
            marker = DEMO._read_marker(root)
            time.sleep(0.18)
            _tmux_run(
                str(marker["real_tmux"]),
                ["clear-history", "-t", REAL_SESSION_BY_AGENT[name]],
                check=False,
            )


def _message(
    root: Path,
    *,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    importance: str,
    created: datetime,
    topic: str,
) -> None:
    db = root / "mail" / "storage.sqlite3"
    with sqlite3.connect(db, timeout=4) as con:
        con.execute("PRAGMA busy_timeout=4000")
        row = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM messages").fetchone()
        message_id = int(row[0])
        sender_id = int(AGENTS[sender]["id"])
        recipient_id = int(AGENTS[recipient]["id"])
        con.execute(
            """
            INSERT INTO messages(
                id, project_id, sender_id, thread_id, topic, subject,
                body_md, importance, ack_required, created_ts
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id, sender_id, f"reel-{topic}", "concept-reel",
                subject, body, importance, 1 if importance in {"high", "urgent"} else 0,
                _iso(created),
            ),
        )
        con.execute(
            "INSERT INTO message_recipients(message_id, agent_id, kind, read_ts, ack_ts) VALUES (?, ?, 'to', NULL, NULL)",
            (message_id, recipient_id),
        )
        stamp = _iso(created)
        con.execute(
            "UPDATE agents SET last_active_ts = ? WHERE id IN (?, ?)",
            (stamp, sender_id, recipient_id),
        )
    _touch_session(root, sender)
    _touch_session(root, recipient)


def _apply_event(root: Path, event: dict[str, Any], anchor: datetime, ordinal: int) -> None:
    event_type = str(event["type"])
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown timeline primitive: {event_type}")
    created = anchor + timedelta(seconds=float(event["at"]), microseconds=ordinal)
    if event_type == "agent_add":
        name = str(event["agent"])
        data = AGENTS[name]
        with sqlite3.connect(root / "mail" / "storage.sqlite3", timeout=4) as con:
            con.execute(
                """
                INSERT INTO agents(
                    id, project_id, name, model, program, task_description,
                    inception_ts, last_active_ts, retired_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (data["id"], name, data["model"], data["program"], data["task"], _iso(created), _iso(created)),
            )
        annot = _annotations(root)
        annot["agents"][name] = {
            "role": data["role"], "emoji": data["emoji"], "group": data["group"],
        }
        _atomic_json(root / "runtime" / "annotations.json", annot)
        _touch_session(root, name)
        return
    if event_type == "spawn":
        child = str(event["child"])
        _message(
            root, sender=str(event["parent"]), recipient=child,
            subject=f"Task {AGENTS[child]['id']}: {AGENTS[child]['task']}",
            body="Start this fictional observatory task and coordinate across the demo network.",
            importance="high", created=created, topic=f"spawn-{AGENTS[child]['id']}",
        )
        return
    if event_type == "mail":
        _message(
            root, sender=str(event["sender"]), recipient=str(event["recipient"]),
            subject=str(event["subject"]), body=str(event["body"]),
            importance=str(event.get("importance") or "normal"), created=created,
            topic=f"mail-{ordinal}",
        )
        return
    name = str(event["agent"])
    capture = _state_capture(name, str(event["act_state"]), int(event["ctx_used"]))
    _touch_session(root, name, capture=capture)


def _reset_story(root: Path, anchor: datetime) -> int:
    with sqlite3.connect(root / "mail" / "storage.sqlite3", timeout=4) as con:
        con.execute("PRAGMA busy_timeout=4000")
        con.execute("DELETE FROM message_recipients")
        con.execute("DELETE FROM messages")
        con.execute("DELETE FROM agents")
    _atomic_json(root / "runtime" / "annotations.json", {"agents": {}})
    _atomic_json(_session_state_path(root), {"version": 0, "sessions": {}})
    for name in REAL_AGENT_NAMES:
        _write_printer(root, name, _default_capture(name))
    applied = 0
    for ordinal, event in enumerate(TIMELINE, start=1):
        if float(event["at"]) > 0:
            break
        _apply_event(root, event, anchor, ordinal)
        applied += 1
    _atomic_json(
        _status_path(root),
        {"profile": PROFILE, "phase": "ready", "elapsed": 0.0,
         "caption": _current_caption(0.0), "applied_events": applied,
         "duration_seconds": STORY_SECONDS},
    )
    return applied


def _server_env(root: Path, port: int, token: str) -> dict[str, str]:
    env = DEMO._server_env(root, port, token)
    env["ORRERY_MAIL_DB"] = str(root / "mail" / "storage.sqlite3")
    return env


def _wait_server(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 15
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"demo server exited early with status {process.returncode}")
        try:
            info = DEMO._get_json(port, "/api/demo-info")
            if info.get("profile") == PROFILE:
                return
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.15)
    raise RuntimeError(f"demo server did not become ready: {last_error}")


def _read_marker(root: Path) -> dict[str, Any]:
    marker = DEMO._read_marker(root)
    if marker.get("profile") != PROFILE:
        raise ValueError(f"not an AgentStack concept reel directory: {root}")
    return marker


def _reel_down(root: Path) -> dict[str, Any]:
    if not root.exists():
        return {"ok": True, "removed": False, "install_dir": str(root)}
    marker = _read_marker(root)
    DEMO._stop_owned_process(root, marker)
    real_tmux = str(marker.get("real_tmux") or _real_tmux())
    recorded = tuple(marker.get("real_sessions") or ())
    if recorded != REAL_SESSION_NAMES:
        raise RuntimeError("refusing tmux cleanup: ownership marker session list changed")
    _kill_real_sessions(real_tmux, recorded)
    DEMO._remove_owned_entries(root)
    return {
        "ok": True, "removed": True, "install_dir": str(root),
        "removed_tmux_sessions": list(recorded),
    }


def up(root: Path, port: int) -> dict[str, Any]:
    if root.exists() and (root / DEMO.MARKER_NAME).is_file():
        existing = DEMO._read_marker(root)
        if existing.get("profile") == PROFILE:
            _reel_down(root)
        else:
            DEMO._reset(root)
    else:
        DEMO._reset(root)
    DEMO._assert_port_available(port)
    real_tmux = _real_tmux()
    root.mkdir(parents=True)
    (root / "home").mkdir()
    marker = DEMO._write_marker(root, port)
    marker.update(
        profile=PROFILE, real_tmux=real_tmux,
        real_sessions=list(REAL_SESSION_NAMES),
        real_session_by_agent=REAL_SESSION_BY_AGENT,
    )
    DEMO._update_marker(root, marker)
    process: subprocess.Popen[bytes] | None = None
    try:
        DEMO._copy_tracked_payload(root)
        (root / "project").mkdir(parents=True, exist_ok=True)
        _create_database(root)
        _create_runtime(root)
        _create_tmux_adapter(root, real_tmux)
        _start_real_sessions(root, real_tmux)
        anchor = datetime.now(timezone.utc)
        _reset_story(root, anchor)
        wrapper = _create_reel_server(root)
        launcher_log = (root / "runtime" / "launcher.log").open("ab")
        try:
            process = subprocess.Popen(
                [sys.executable, str(wrapper)], cwd=root / "project",
                env=_server_env(root, port, marker["control_token"]),
                stdin=subprocess.DEVNULL, stdout=launcher_log,
                stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
            )
        finally:
            launcher_log.close()
        marker["server_pid"] = process.pid
        DEMO._update_marker(root, marker)
        _wait_server(port, process)
        initial = _verify_snapshot(root, port, expect_final=False)
        return {
            "ok": True,
            "url": f"http://127.0.0.1:{port}/?view=net",
            "terminal_session": REAL_SESSION_BY_AGENT[REAL_AGENT_NAMES[0]],
            "install_dir": str(root),
            "dashboard_url": f"http://127.0.0.1:{port}",
            "orrery_mail_db": str(root / "mail" / "storage.sqlite3"),
            "real_tmux_sessions": list(REAL_SESSION_NAMES),
            "tmux_session_by_agent": REAL_SESSION_BY_AGENT,
            "initial": initial,
            "play": f"{Path(__file__).resolve()} play --install-dir {root}",
            "cleanup": f"{Path(__file__).resolve()} down --install-dir {root}",
        }
    except Exception:
        if process is not None:
            try:
                current = DEMO._read_marker(root)
                DEMO._stop_owned_process(root, current)
            except Exception:
                pass
        try:
            _kill_real_sessions(real_tmux)
        except Exception:
            pass
        raise


def _capture_real(tmux_bin: str, name: str) -> str:
    result = _tmux_run(
        # capture-pane takes a pane target; unlike has-session/kill-session it
        # does not accept tmux's exact-session '=' prefix.
        tmux_bin, ["capture-pane", "-p", "-J", "-t", name, "-S", "-45"],
    )
    return result.stdout


def _sample(port: int, since: int) -> tuple[int, set[str], int]:
    agents = DEMO._get_json(port, "/api/agents").get("agents", [])
    states = {
        str(agent.get("act_state"))
        for agent in agents if agent.get("act_state") in {"work", "question", "ask", "wait"}
    }
    messages = DEMO._get_json(
        port, f"/api/messages-since?since={since}&limit=200"
    ).get("messages", [])
    return len(agents), states, len(messages)


def play(root: Path, port: int | None) -> dict[str, Any]:
    marker = _read_marker(root)
    actual_port = DEMO._validate_port(int(port if port is not None else marker["port"]))
    if port is not None and actual_port != int(marker["port"]):
        raise ValueError(f"port mismatch: marker has {marker['port']}, argument has {port}")
    anchor = datetime.now(timezone.utc)
    applied = _reset_story(root, anchor)
    start = time.monotonic()
    since = int(anchor.timestamp()) - 2
    next_sample = start
    agent_counts: set[int] = set()
    act_states: set[str] = set()
    message_counts: list[int] = []

    def observe(force: bool = False) -> None:
        nonlocal next_sample
        now = time.monotonic()
        if not force and now < next_sample:
            return
        count, states, messages = _sample(actual_port, since)
        agent_counts.add(count)
        act_states.update(states)
        if not message_counts or message_counts[-1] != messages:
            message_counts.append(messages)
        next_sample = now + 0.45

    for ordinal, event in enumerate(TIMELINE, start=1):
        if float(event["at"]) <= 0:
            continue
        deadline = start + float(event["at"])
        while time.monotonic() < deadline:
            observe()
            time.sleep(min(0.08, max(0.0, deadline - time.monotonic())))
        _apply_event(root, event, anchor, ordinal)
        applied += 1
        elapsed = min(STORY_SECONDS, time.monotonic() - start)
        _atomic_json(
            _status_path(root),
            {"profile": PROFILE, "phase": "playing", "elapsed": round(elapsed, 3),
             "caption": _current_caption(elapsed), "applied_events": applied,
             "duration_seconds": STORY_SECONDS},
        )
        observe(force=True)
    end = start + STORY_SECONDS
    while time.monotonic() < end:
        observe()
        time.sleep(min(0.08, max(0.0, end - time.monotonic())))
    # One final poll after the runtime parser's 4.5-second cache had a chance
    # to sample the 21s states.  The per-session state transitions are all 6s+
    # apart, matching the verified dashboard contract.
    observe(force=True)
    _atomic_json(
        _status_path(root),
        {"profile": PROFILE, "phase": "complete", "elapsed": STORY_SECONDS,
         "caption": _current_caption(STORY_SECONDS), "applied_events": applied,
         "duration_seconds": STORY_SECONDS},
    )
    snapshot = _verify_snapshot(root, actual_port, expect_final=True)
    missing_counts = {1, 3, 6, 9, 12} - agent_counts
    missing_states = {"work", "question", "ask", "wait"} - act_states
    if missing_counts:
        raise RuntimeError(f"API timeline missed agent counts: {sorted(missing_counts)}")
    if missing_states:
        raise RuntimeError(f"API timeline missed act_state values: {sorted(missing_states)}")
    return {
        "ok": True, "duration_seconds": STORY_SECONDS,
        "observed_agent_counts": sorted(agent_counts),
        "observed_act_states": sorted(act_states),
        "observed_message_counts": message_counts,
        "final": snapshot,
    }


def frame(root: Path, port: int | None, at: float) -> dict[str, Any]:
    """Hold one deterministic story frame for filming or visual diagnosis."""
    if not 0.0 <= at <= STORY_SECONDS:
        raise ValueError(f"--at must be between 0 and {STORY_SECONDS:g}")
    marker = _read_marker(root)
    actual_port = DEMO._validate_port(int(port if port is not None else marker["port"]))
    anchor = datetime.now(timezone.utc) - timedelta(seconds=at)
    applied = _reset_story(root, anchor)
    for ordinal, event in enumerate(TIMELINE, start=1):
        offset = float(event["at"])
        if offset <= 0:
            continue
        if offset > at:
            break
        _apply_event(root, event, anchor, ordinal)
        applied += 1
    _atomic_json(
        _status_path(root),
        {"profile": PROFILE, "phase": "frame", "elapsed": round(at, 3),
         "caption": _current_caption(at), "applied_events": applied,
         "duration_seconds": STORY_SECONDS},
    )
    count, states, messages = _sample(actual_port, int(anchor.timestamp()) - 2)
    return {
        "ok": True, "at": at, "caption": _current_caption(at),
        "agent_count": count, "act_states": sorted(states),
        "message_count": messages,
    }


def _verify_snapshot(root: Path, port: int, *, expect_final: bool) -> dict[str, Any]:
    agents_payload = DEMO._get_json(port, "/api/agents")
    agents = agents_payload.get("agents", [])
    graph = DEMO._get_json(port, "/api/graph?all=1")
    since = int(time.time()) - 300
    messages = DEMO._get_json(port, f"/api/messages-since?since={since}&limit=200")
    names = {str(agent.get("name")) for agent in agents}
    expected_names = set(AGENTS) if expect_final else {"Bright-Curie"}
    if names != expected_names:
        raise RuntimeError(f"unexpected /api/agents names: {sorted(names)}")
    graph_names = {str(node.get("name")) for node in graph.get("nodes", [])}
    if graph_names != expected_names:
        raise RuntimeError(f"unexpected /api/graph names: {sorted(graph_names)}")
    if expect_final:
        spawn = {(item["source"], item["target"]) for item in graph.get("spawn", [])}
        if spawn != EXPECTED_SPAWN:
            raise RuntimeError(f"unexpected spawn lineage: {sorted(spawn)}")
        if len(messages.get("messages", [])) != EXPECTED_MESSAGES:
            raise RuntimeError(
                f"expected {EXPECTED_MESSAGES} comet messages, got {len(messages.get('messages', []))}"
            )
    real_tmux = str(_read_marker(root)["real_tmux"])
    captures: dict[str, int] = {}
    for name in REAL_AGENT_NAMES:
        session_name = REAL_SESSION_BY_AGENT[name]
        if not _has_real_session(real_tmux, session_name):
            raise RuntimeError(f"real tmux printer is missing: {session_name}")
        env = _tmux_run(real_tmux, ["show-environment", "-t", f"={session_name}", "CLAUDECODE"])
        if "CLAUDECODE=1" not in env.stdout:
            raise RuntimeError(f"CLAUDECODE safety env missing from {session_name}")
        capture = _capture_real(real_tmux, session_name)
        if "AgentStack concept reel" not in capture:
            raise RuntimeError(f"real tmux printer is empty: {session_name}")
        captures[name] = len(capture.rstrip().splitlines())
    return {
        "agent_count": len(agents), "graph_node_count": len(graph_names),
        "spawn_count": len(graph.get("spawn", [])),
        "message_count": len(messages.get("messages", [])),
        "real_tmux_capture_lines": captures,
    }


def verify(root: Path, port: int | None) -> dict[str, Any]:
    marker = _read_marker(root)
    actual_port = DEMO._validate_port(int(port if port is not None else marker["port"]))
    status = _read_json(_status_path(root), {})
    snapshot = _verify_snapshot(root, actual_port, expect_final=status.get("phase") == "complete")
    return {"ok": True, "status": status, "snapshot": snapshot}


def status(root: Path, port: int | None) -> dict[str, Any]:
    marker = _read_marker(root)
    actual_port = DEMO._validate_port(int(port if port is not None else marker["port"]))
    return {
        "ok": True, "url": f"http://127.0.0.1:{actual_port}/?view=net",
        "dashboard_url": f"http://127.0.0.1:{actual_port}",
        "orrery_mail_db": str(root / "mail" / "storage.sqlite3"),
        "terminal_session": REAL_SESSION_BY_AGENT[REAL_AGENT_NAMES[0]],
        "tmux_session_by_agent": REAL_SESSION_BY_AGENT,
        "story": DEMO._get_json(actual_port, "/api/demo-reel"),
    }


def timeline_payload() -> dict[str, Any]:
    return {
        "duration_seconds": STORY_SECONDS,
        "primitive_types": sorted(EVENT_TYPES),
        "captions": CAPTIONS,
        "events": TIMELINE,
        "tmux_session_by_agent": REAL_SESSION_BY_AGENT,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    up_parser = sub.add_parser("up", help="prepare isolated dashboard, DB, and three tmux printers")
    up_parser.add_argument("--install-dir", default=str(DEFAULT_INSTALL_DIR))
    up_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    play_parser = sub.add_parser("play", help="reset and run the real-time 25-second story")
    play_parser.add_argument("--install-dir", default=str(DEFAULT_INSTALL_DIR))
    play_parser.add_argument("--port", type=int)
    frame_parser = sub.add_parser("frame", help="hold a deterministic timeline frame")
    frame_parser.add_argument("--install-dir", default=str(DEFAULT_INSTALL_DIR))
    frame_parser.add_argument("--port", type=int)
    frame_parser.add_argument("--at", required=True, type=float)
    verify_parser = sub.add_parser("verify", help="verify the current initial/final story snapshot")
    verify_parser.add_argument("--install-dir", default=str(DEFAULT_INSTALL_DIR))
    verify_parser.add_argument("--port", type=int)
    status_parser = sub.add_parser("status", help="print ORRERY endpoints and current reel state")
    status_parser.add_argument("--install-dir", default=str(DEFAULT_INSTALL_DIR))
    status_parser.add_argument("--port", type=int)
    down_parser = sub.add_parser("down", help="remove only marker-owned demo data and tmux sessions")
    down_parser.add_argument("--install-dir", default=str(DEFAULT_INSTALL_DIR))
    sub.add_parser("timeline", help="print the declarative story JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "timeline":
            result = timeline_payload()
        else:
            root = DEMO._install_dir(args.install_dir)
            if args.command == "up":
                result = up(root, DEMO._validate_port(args.port))
            elif args.command == "play":
                result = play(root, args.port)
            elif args.command == "frame":
                result = frame(root, args.port, args.at)
            elif args.command == "verify":
                result = verify(root, args.port)
            elif args.command == "status":
                result = status(root, args.port)
            else:
                result = _reel_down(root)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, urllib.error.URLError) as exc:
        print(f"dashboard-demo-reel: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
