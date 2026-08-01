#!/usr/bin/env python3
"""Create a deterministic, isolated dashboard environment for documentation.

The demo never reads the user's AgentStack install, agent-mail database, or
tmux server.  It copies only git-tracked dashboard payload files, creates a
small fictional SQLite database, and places a deterministic tmux shim first on
PATH.  The HTTP server is wrapped in read-only mode so a misplaced click cannot
spawn, retire, annotate, or attach to a real agent.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DASHBOARD = REPO_ROOT / "dashboard"
MARKER_NAME = ".agentstack-dashboard-demo.json"
MARKER_KIND = "agentstack-dashboard-docs-demo"
FIXTURE_SEED = "aurora-terrarium-v1"
DEFAULT_PORT = 8877
LIVE_PORT = 8770
OWNED_TOP_LEVEL = {
    MARKER_NAME,
    "bin",
    "home",
    "mail",
    "payload",
    "project",
    "runtime",
}

AGENTS = (
    {
        "id": 1,
        "name": "Bright-Curie",
        "model": "claude-opus-4-7[1m]",
        "program": "claude-code",
        "task": "Coordinate the fictional Aurora Terrarium exhibit launch",
        "inception_min": 0,
        "last_active_min": 0,
        "retired_min": None,
    },
    {
        "id": 2,
        "name": "Swift-Noether",
        "model": "gpt-5.6-sol",
        "program": "codex-cli",
        "task": "Design fictional greenhouse telemetry cards and color tokens",
        "inception_min": 2,
        "last_active_min": 1,
        "retired_min": None,
    },
    {
        "id": 3,
        "name": "Calm-Turing",
        "model": "claude-sonnet-5",
        "program": "claude-code",
        "task": "Write a fictional visitor journey for the orbital greenhouse",
        "inception_min": 4,
        "last_active_min": 8,
        "retired_min": None,
    },
    {
        "id": 4,
        "name": "Quiet-Franklin",
        "model": "claude-haiku-4-5-20251001",
        "program": "claude-code",
        "task": "Archive fictional exhibit notes and accessibility captions",
        "inception_min": 6,
        "last_active_min": 16,
        "retired_min": 12,
    },
)

MESSAGES = (
    (1, 1, 2, "high", "Task A: telemetry cards", "Create three fictional telemetry cards for the Aurora Terrarium. Use invented sensor readings only.", "demo-telemetry"),
    (2, 1, 3, "high", "Task B: visitor journey", "Draft a fictional three-stop visitor journey: airlock, canopy, and night garden.", "demo-journey"),
    (3, 1, 4, "urgent", "Task C: archive captions", "Prepare invented captions and a compact archive index for the demo exhibit.", "demo-archive"),
    (4, 2, 1, "normal", "Telemetry palette ready", "The fictional humidity, light, and nutrient cards now share one accessible color system.", "demo-telemetry"),
    (5, 3, 1, "normal", "Journey draft ready", "The invented visitor route now has a clear beginning, midpoint, and quiet ending.", "demo-journey"),
    (6, 2, 3, "normal", "Coordinate card labels", "Can the canopy stop reuse the same fictional humidity label as the telemetry deck?", "demo-crosscheck"),
    (7, 3, 2, "normal", "Re: Coordinate card labels", "Yes. I will call it Canopy Humidity and keep the invented value at sixty-eight percent.", "demo-crosscheck"),
    (8, 4, 2, "normal", "Caption handoff", "The demo captions are fictional and ready for the telemetry card alt text.", "demo-handoff"),
    (9, 2, 4, "normal", "Re: Caption handoff", "Received. I added the fictional captions to the card checklist.", "demo-handoff"),
    (10, 1, 2, "high", "Polish the night-garden card", "Please emphasize the invented bioluminescence trend and keep the copy under two lines.", "demo-telemetry"),
    (11, 2, 1, "normal", "Night-garden card complete", "The fictional trend is now the visual focus and the supporting copy fits in one line.", "demo-telemetry"),
    (12, 3, 1, "normal", "Visitor journey complete", "All three fictional stops are ready for the documentation walkthrough.", "demo-journey"),
    (13, 1, 4, "normal", "Archive accepted", "The invented captions are approved. This demo branch can now be retired.", "demo-archive"),
    (14, 4, 3, "normal", "Accessibility note", "The fictional night-garden stop includes a non-color cue for every status chip.", "demo-accessibility"),
)

ANNOTATIONS = {
    "Bright-Curie": {"role": "Demo director", "emoji": "🧭", "group": "Aurora crew"},
    "Swift-Noether": {"role": "Visual systems", "emoji": "🎛️", "group": "Aurora crew"},
    "Calm-Turing": {"role": "Story architect", "emoji": "🪴", "group": "Visitor path"},
    "Quiet-Franklin": {"role": "Archive curator", "emoji": "🗂️", "group": "Visitor path"},
}

TMUX_SESSIONS = {
    "Bright-Curie": {
        "cmd": "claude",
        "title": "✻ Coordinating the Aurora Terrarium launch",
        "capture": """Aurora Terrarium demo coordination\n\n✻ Crunched for 1m 36s\n| Opus 4.7 | ctx: 38% used | (1M context)\n""",
    },
    "Swift-Noether": {
        "cmd": "zsh",
        "title": "• Simulating color-coded telemetry cards",
        "capture": """Telemetry card preview uses fictional readings only.\n\n• Working (18s • esc to interrupt)\ngpt-5.6-sol xhigh · Context 72% left · /demo/aurora-terrarium\n""",
    },
    "Calm-Turing": {
        "cmd": "zsh",
        "title": "zsh",
        "capture": "Visitor journey draft finished.\n",
    },
}


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _install_dir(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("--install-dir must be an absolute path")
    path = path.resolve(strict=False)
    home = Path.home().resolve()
    forbidden_exact = {
        Path("/").resolve(),
        home,
        Path("/tmp").resolve(),
        REPO_ROOT.resolve(),
    }
    if path in forbidden_exact:
        raise ValueError(f"refusing dangerous --install-dir: {path}")
    for protected in (home / ".agentstack", home / "mcp_agent_mail", home / ".mcp_agent_mail"):
        protected = protected.resolve(strict=False)
        if path == protected or protected in path.parents:
            raise ValueError(f"demo must not live inside real runtime path: {protected}")
    if REPO_ROOT.resolve() in path.parents:
        raise ValueError("demo must not be created inside the source repository")
    return path


def _validate_port(port: int) -> int:
    if port == LIVE_PORT:
        raise ValueError(f"port {LIVE_PORT} is reserved for the live dashboard")
    if not 1024 <= port <= 65535:
        raise ValueError("--port must be between 1024 and 65535")
    return port


def _marker_path(root: Path) -> Path:
    return root / MARKER_NAME


def _read_marker(root: Path) -> dict[str, Any]:
    path = _marker_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"not an AgentStack dashboard demo directory: {root}") from exc
    if data.get("kind") != MARKER_KIND or data.get("install_dir") != str(root):
        raise ValueError(f"invalid demo ownership marker: {path}")
    return data


def _write_marker(root: Path, port: int) -> dict[str, Any]:
    data = {
        "kind": MARKER_KIND,
        "fixture_seed": FIXTURE_SEED,
        "install_dir": str(root),
        "port": port,
        "control_token": os.urandom(24).hex(),
        "server_pid": None,
    }
    path = _marker_path(root)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return data


def _update_marker(root: Path, data: dict[str, Any]) -> None:
    tmp = root / f"{MARKER_NAME}.tmp"
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, _marker_path(root))


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _stop_owned_process(root: Path, marker: dict[str, Any]) -> None:
    try:
        pid = int(marker.get("server_pid") or 0)
    except (TypeError, ValueError):
        return
    if not _pid_alive(pid):
        return
    port = _validate_port(int(marker.get("port") or 0))
    token = str(marker.get("control_token") or "")
    try:
        info = _get_json(port, "/api/demo-info")
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise RuntimeError(
            f"refusing to signal pid {pid}: demo ownership endpoint is unavailable"
        ) from exc
    if info.get("kind") != MARKER_KIND or info.get("fixture_seed") != FIXTURE_SEED:
        raise RuntimeError(f"refusing to signal pid {pid}: ownership response did not match")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/__demo_shutdown__",
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-AgentStack-Demo-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            if response.status != 200:
                raise RuntimeError(f"demo shutdown returned HTTP {response.status}")
    except urllib.error.URLError as exc:
        raise RuntimeError("demo shutdown request failed") from exc
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.1)
    if _pid_alive(pid):
        # The authenticated ownership endpoint above proved this is our
        # process.  A signal is now safe even when `ps` is sandbox-blocked.
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.1)
    if _pid_alive(pid):
        raise RuntimeError(f"demo runner pid {pid} did not stop")


def _remove_owned_entries(root: Path) -> None:
    if not root.exists():
        return
    unknown = {entry.name for entry in root.iterdir()} - OWNED_TOP_LEVEL
    if unknown:
        raise ValueError(
            "refusing to reset demo directory with unowned entries: "
            + ", ".join(sorted(unknown))
        )
    for name in sorted(OWNED_TOP_LEVEL - {MARKER_NAME}):
        path = root / name
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
    _marker_path(root).unlink(missing_ok=True)
    try:
        root.rmdir()
    except OSError:
        pass


def _reset(root: Path) -> None:
    if not root.exists():
        return
    if not _marker_path(root).is_file():
        if any(root.iterdir()):
            raise ValueError(f"refusing to overwrite non-demo directory: {root}")
        root.rmdir()
        return
    marker = _read_marker(root)
    _stop_owned_process(root, marker)
    _remove_owned_entries(root)


def _copy_tracked_payload(root: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", "dashboard", "bin/lib/agentstack-scientists.sh"],
        capture_output=True,
        check=True,
    )
    payload = root / "payload"
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        rel = Path(os.fsdecode(raw))
        source = REPO_ROOT / rel
        target = payload / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _create_database(root: Path, now: datetime) -> None:
    mail_dir = root / "mail"
    mail_dir.mkdir(parents=True, exist_ok=True)
    db_path = mail_dir / "storage.sqlite3"
    project_key = str(root / "project")
    base = now.replace(second=0, microsecond=0) - timedelta(minutes=35)
    with sqlite3.connect(db_path) as con:
        con.executescript(
            """
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
        con.execute(
            "INSERT INTO projects(id, human_key, slug, created_at) VALUES (?, ?, ?, ?)",
            (1, project_key, "fictional-aurora-terrarium", _iso(base)),
        )
        for agent in AGENTS:
            retired_at = (
                _iso(now - timedelta(minutes=int(agent["retired_min"])))
                if agent["retired_min"] is not None
                else None
            )
            con.execute(
                """
                INSERT INTO agents(
                    id, project_id, name, model, program, task_description,
                    inception_ts, last_active_ts, retired_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent["id"], agent["name"], agent["model"], agent["program"],
                    agent["task"], _iso(base + timedelta(minutes=int(agent["inception_min"]))),
                    _iso(now - timedelta(minutes=int(agent["last_active_min"]))), retired_at,
                ),
            )
        for offset, (message_id, sender, recipient, importance, subject, body, thread_id) in enumerate(MESSAGES, start=1):
            created = base + timedelta(minutes=6 + offset * 2)
            con.execute(
                """
                INSERT INTO messages(
                    id, project_id, sender_id, thread_id, topic, subject,
                    body_md, importance, ack_required, created_ts
                ) VALUES (?, 1, ?, ?, 'docs-demo', ?, ?, ?, ?, ?)
                """,
                (
                    message_id, sender, thread_id, subject, body, importance,
                    1 if importance in {"high", "urgent"} else 0, _iso(created),
                ),
            )
            con.execute(
                """
                INSERT INTO message_recipients(message_id, agent_id, kind, read_ts, ack_ts)
                VALUES (?, ?, 'to', ?, ?)
                """,
                (
                    message_id, recipient, _iso(created + timedelta(minutes=1)),
                    _iso(created + timedelta(minutes=2)) if importance in {"high", "urgent"} else None,
                ),
            )


def _create_runtime(root: Path) -> None:
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "annotations.json").write_text(
        json.dumps({"agents": ANNOTATIONS}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _create_deliverables(root: Path) -> None:
    logs = root / "project" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    items = (
        ("Bright-Curie", "LOG_2026-08-02T0900 Aurora Demo Direction.md", "Fictional shot list for the Aurora Terrarium dashboard demo."),
        ("Swift-Noether", "LOG_2026-08-02T0915 Telemetry Cards.md", "Invented greenhouse telemetry card notes."),
        ("Calm-Turing", "LOG_2026-08-02T0930 Visitor Journey.md", "Fictional visitor journey copy."),
    )
    for agent, filename, body in items:
        (logs / filename).write_text(
            f"---\nagent: {agent}\n---\n\n# Fictional demo artifact\n\n{body}\n",
            encoding="utf-8",
        )


def _create_command_shims(root: Path, now: datetime) -> None:
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    sessions = {
        name: {
            **data,
            "created": int((now - timedelta(minutes=40 - index * 3)).timestamp()),
            "activity": int((now - timedelta(seconds=15 + index * 80)).timestamp()),
        }
        for index, (name, data) in enumerate(TMUX_SESSIONS.items())
    }
    tmux_script = f"""#!/usr/bin/env python3
import json
import sys

SESSIONS = json.loads({json.dumps(json.dumps(sessions, ensure_ascii=False))})
SEP = chr(31)
args = sys.argv[1:]
command = args[0] if args else ""
if command == "list-sessions":
    for name, data in SESSIONS.items():
        print(SEP.join((name, str(data["created"]), str(data["activity"]))))
elif command == "list-panes":
    for name, data in SESSIONS.items():
        print(SEP.join((name, "11", data["cmd"], data["title"])))
elif command == "list-clients":
    print(SEP.join(("Bright-Curie", "/dev/ttys-demo")))
elif command == "capture-pane":
    target = args[args.index("-t") + 1] if "-t" in args else ""
    target = target.lstrip("=")
    data = SESSIONS.get(target)
    if data is None:
        raise SystemExit(1)
    print(data["capture"], end="")
elif command == "has-session":
    target = args[args.index("-t") + 1] if "-t" in args else ""
    raise SystemExit(0 if target.lstrip("=") in SESSIONS else 1)
else:
    raise SystemExit(1)
"""
    tmux = bin_dir / "tmux"
    tmux.write_text(tmux_script, encoding="utf-8")
    tmux.chmod(0o755)
    for command in ("launchctl", "pkill", "ttyd"):
        shim = bin_dir / command
        shim.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        shim.chmod(0o755)


def _create_read_only_server(root: Path) -> Path:
    wrapper = root / "payload" / "demo_server.py"
    wrapper.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

dashboard = Path(__file__).resolve().parent / "dashboard"
sys.path.insert(0, str(dashboard))
import server

class ReadOnlyDemoHandler(server.Handler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/demo-info":
            self._send(200, json.dumps({"kind": "agentstack-dashboard-docs-demo", "fixture_seed": "aurora-terrarium-v1"}).encode(), "application/json; charset=utf-8")
            return
        if path == "/api/ptty":
            self._send(403, json.dumps({"ok": False, "error": "read-only documentation demo"}).encode(), "application/json; charset=utf-8")
            return
        super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path == "/__demo_shutdown__":
            if self.headers.get("X-AgentStack-Demo-Token", "") != os.environ.get("AGENTSTACK_DEMO_CONTROL_TOKEN", ""):
                self._send(403, json.dumps({"ok": False, "error": "invalid demo control token"}).encode(), "application/json; charset=utf-8")
                return
            self._send(200, json.dumps({"ok": True}).encode(), "application/json; charset=utf-8")
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        self._send(403, json.dumps({"ok": False, "error": "read-only documentation demo"}).encode(), "application/json; charset=utf-8")

def main():
    srv = server.ThreadingHTTPServer((server.BIND_HOST, server.PORT), ReadOnlyDemoHandler)
    print(f"agent-dashboard demo listening on http://{server.BIND_HOST}:{server.PORT}/", flush=True)
    srv.serve_forever()
    srv.server_close()

if __name__ == "__main__":
    main()
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def _server_env(root: Path, port: int, control_token: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(root / "home"),
            "PATH": os.pathsep.join((str(root / "bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin")),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMUX": "",
            "AGENTSTACK_PORT": str(port),
            "AGENTSTACK_BIND_HOST": "127.0.0.1",
            "AGENTSTACK_MAIL_DB": str(root / "mail" / "storage.sqlite3"),
            "AGENTSTACK_MAIL_ENV": str(root / "mail" / ".env"),
            "AGENTSTACK_MAIL_HOME": str(root / "mail"),
            "AGENTSTACK_PROJECT_KEY": str(root / "project"),
            "AGENTSTACK_VAULT": "",
            "AGENTSTACK_RUNTIME_DIR": str(root / "runtime"),
            "AGENTSTACK_HOOKS_DIR": str(root / "payload" / "hooks"),
            "AGENTSTACK_SIGNALS_DIR": str(root / "mail" / "signals"),
            "AGENTSTACK_DELIVERABLE_ROOTS": str(root / "project" / "logs"),
            "AGENTSTACK_SPAWN_ROOTS": str(root / "project"),
            "AGENTSTACK_SPAWN_DIRS": str(root / "project"),
            "AGENTSTACK_SPAWN_SCRIPT": str(root / "payload" / "disabled-demo-spawn"),
            "AGENTSTACK_DASHBOARD_LOG": str(root / "runtime" / "dashboard.log"),
            "AGENTSTACK_DASHBOARD_STATE": str(root / "runtime" / "dashboard-service-state.json"),
            "AGENTSTACK_DASHBOARD_RESTART_DELAY": "1",
            "AGENTSTACK_DASHBOARD_LOG_MAX_BYTES": str(2 * 1024 * 1024),
            "AGENTSTACK_DASHBOARD_LOG_BACKUPS": "2",
            "AGENTSTACK_DEMO_CONTROL_TOKEN": control_token,
        }
    )
    return env


def _assert_port_available(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # Match ThreadingHTTPServer's SO_REUSEADDR behavior so an idempotent
        # `up` is not rejected by the previous demo's short TIME_WAIT tail.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"port {port} is already in use") from exc


def _get_json(port: int, path: str, timeout: float = 3) -> dict[str, Any]:
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


def _verify(port: int) -> dict[str, Any]:
    agents_payload = _get_json(port, "/api/agents")
    graph = _get_json(port, "/api/graph?all=1")
    annotations = _get_json(port, "/api/annotations")
    spawn_names = _get_json(port, "/api/spawn-names")
    replay = _get_json(
        port,
        "/api/agent-history?"
        + urllib.parse.urlencode(
            {
                "names": ",".join(agent["name"] for agent in AGENTS),
                "hours": "24",
            }
        ),
    )
    recent_messages = _get_json(port, "/api/messages-since?since=0&limit=80")
    edge = _get_json(
        port,
        "/api/edge-messages?"
        + urllib.parse.urlencode({"a": "Swift-Noether", "b": "Calm-Turing", "limit": 60}),
    )
    agents = {row["name"]: row for row in agents_payload.get("agents", [])}
    expected_names = {agent["name"] for agent in AGENTS}
    if set(agents) != expected_names:
        raise RuntimeError(f"unexpected /api/agents names: {sorted(agents)}")
    expected_categories = {
        "Bright-Curie": ("agent", True),
        "Swift-Noether": ("agent", True),
        "Calm-Turing": ("finished", False),
        "Quiet-Franklin": ("retired", False),
    }
    for name, (category, running) in expected_categories.items():
        row = agents[name]
        if row.get("category") != category or bool(row.get("running")) != running:
            raise RuntimeError(f"unexpected state for {name}: {row.get('category')}/{row.get('running')}")
        if not row.get("task"):
            raise RuntimeError(f"missing fictional task for {name}")
    for name in ("Bright-Curie", "Swift-Noether"):
        if agents[name].get("ctx_used") is None:
            raise RuntimeError(f"missing context reading for running agent {name}")
    graph_names = {node["name"] for node in graph.get("nodes", [])}
    if graph_names != expected_names:
        raise RuntimeError(f"unexpected /api/graph names: {sorted(graph_names)}")
    expected_spawn = {
        ("Bright-Curie", "Swift-Noether"),
        ("Bright-Curie", "Calm-Turing"),
        ("Bright-Curie", "Quiet-Franklin"),
    }
    actual_spawn = {(item["source"], item["target"]) for item in graph.get("spawn", [])}
    if actual_spawn != expected_spawn:
        raise RuntimeError(f"unexpected spawn lineage: {sorted(actual_spawn)}")
    communication_count = sum(int(item.get("count") or 0) for item in graph.get("edges", []))
    if communication_count != len(MESSAGES):
        raise RuntimeError(f"expected {len(MESSAGES)} message deliveries, got {communication_count}")
    if edge.get("count", 0) < 2:
        raise RuntimeError("child-to-child edge drawer lacks a multi-message exchange")
    annot_names = set((annotations.get("annotations") or {}).keys())
    if annot_names != expected_names:
        raise RuntimeError(f"unexpected annotations: {sorted(annot_names)}")
    scientist_names = {item.get("name") for item in spawn_names.get("names", [])}
    if not {"Curie", "Noether", "Turing", "Franklin"}.issubset(scientist_names):
        raise RuntimeError("NEW AGENT picker is missing the demo scientists")
    if not spawn_names.get("models") or not spawn_names.get("providers"):
        raise RuntimeError("NEW AGENT picker is missing model/provider choices")
    replay_kinds = {event.get("kind") for event in replay.get("events", [])}
    if not replay.get("ok") or not {"spawn", "mail_sent", "retire"}.issubset(replay_kinds):
        raise RuntimeError(f"DIGEST REPLAY lacks demo event types: {sorted(replay_kinds)}")
    if len(replay.get("events", [])) < len(MESSAGES):
        raise RuntimeError("DIGEST REPLAY lacks the fictional message timeline")
    if len(recent_messages.get("messages", [])) < 2:
        raise RuntimeError("NETWORK comet feed lacks recent fictional messages")
    return {
        "ok": True,
        "url": f"http://127.0.0.1:{port}/",
        "agents": {
            name: {
                "category": agents[name]["category"],
                "running": agents[name]["running"],
                "ctx_used": agents[name].get("ctx_used"),
                "task": agents[name]["task"],
            }
            for name in sorted(agents)
        },
        "spawn": sorted([list(item) for item in actual_spawn]),
        "message_deliveries": communication_count,
        "child_exchange_messages": edge["count"],
        "annotations": sorted(annot_names),
        "spawn_picker_scientists": len(scientist_names),
        "replay_events": len(replay["events"]),
        "recent_comet_messages": len(recent_messages["messages"]),
    }


def up(root: Path, port: int) -> dict[str, Any]:
    _reset(root)
    _assert_port_available(port)
    root.mkdir(parents=True)
    (root / "home").mkdir()
    marker = _write_marker(root, port)
    now = datetime.now(timezone.utc)
    try:
        _copy_tracked_payload(root)
        _create_database(root, now)
        _create_runtime(root)
        _create_deliverables(root)
        _create_command_shims(root, now)
        wrapper = _create_read_only_server(root)
        launcher_log = (root / "runtime" / "launcher.log").open("ab")
        try:
            process = subprocess.Popen(
                [sys.executable, str(wrapper)],
                cwd=root / "project",
                env=_server_env(root, port, marker["control_token"]),
                stdin=subprocess.DEVNULL,
                stdout=launcher_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            launcher_log.close()
        marker["server_pid"] = process.pid
        _update_marker(root, marker)
        deadline = time.monotonic() + 15
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"demo runner exited early with status {process.returncode}")
            try:
                result = _verify(port)
                result["install_dir"] = str(root)
                result["cleanup"] = f"{Path(__file__).resolve()} down --install-dir {root}"
                return result
            except (OSError, RuntimeError, ValueError, urllib.error.URLError) as exc:
                last_error = exc
                time.sleep(0.2)
        raise RuntimeError(f"demo did not become healthy: {last_error}")
    except Exception:
        try:
            current = _read_marker(root)
            _stop_owned_process(root, current)
        except Exception:
            pass
        raise


def verify(root: Path, port: int | None) -> dict[str, Any]:
    marker = _read_marker(root)
    actual_port = _validate_port(int(port if port is not None else marker["port"]))
    if port is not None and actual_port != int(marker["port"]):
        raise ValueError(f"port mismatch: marker has {marker['port']}, argument has {port}")
    result = _verify(actual_port)
    result["install_dir"] = str(root)
    return result


def down(root: Path) -> dict[str, Any]:
    if not root.exists():
        return {"ok": True, "removed": False, "install_dir": str(root)}
    marker = _read_marker(root)
    _stop_owned_process(root, marker)
    _remove_owned_entries(root)
    return {"ok": True, "removed": True, "install_dir": str(root)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    up_parser = sub.add_parser("up", help="recreate fixture, start dashboard, and verify APIs")
    up_parser.add_argument("--install-dir", required=True, help="absolute isolated demo directory")
    up_parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"loopback port (default: {DEFAULT_PORT}; {LIVE_PORT} is refused)")
    verify_parser = sub.add_parser("verify", help="verify a running demo")
    verify_parser.add_argument("--install-dir", required=True, help="absolute isolated demo directory")
    verify_parser.add_argument("--port", type=int, help="expected port (defaults to ownership marker)")
    down_parser = sub.add_parser("down", help="stop and remove only marker-owned demo files")
    down_parser.add_argument("--install-dir", required=True, help="absolute isolated demo directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = _install_dir(args.install_dir)
        if args.command == "up":
            result = up(root, _validate_port(args.port))
        elif args.command == "verify":
            result = verify(root, args.port)
        else:
            result = down(root)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"dashboard-demo: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
