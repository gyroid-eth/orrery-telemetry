from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from agentstack_codex_app.hook_entry import (
    append_spool,
    forward_event,
    normalize_event,
    runtime_dir_from_env,
)


def _raw_event():
    return {
        "session_id": "session-example",
        "cwd": "/workspace/example",
        "model": "gpt-example",
        "hook_event_name": "UserPromptSubmit",
        "turn_id": "turn-example",
        "prompt": "private prompt must be dropped",
        "tool_input": {"secret": "must be dropped"},
    }


def test_normalize_event_drops_prompt_and_tool_data():
    event = normalize_event(_raw_event())
    assert event == {
        "schema_version": 1,
        "session_id": "session-example",
        "agent_id": None,
        "cwd": "/workspace/example",
        "model": "gpt-example",
        "hook_event_name": "UserPromptSubmit",
        "turn_id": "turn-example",
    }
    assert "private prompt" not in json.dumps(event)


def test_append_spool_is_private_jsonl(tmp_path):
    path = tmp_path / "runtime" / "events.jsonl"
    append_spool(normalize_event(_raw_event()), path)
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["session_id"] == "session-example"


def test_forward_event_uses_unix_socket():
    with tempfile.TemporaryDirectory(prefix="cas-hook-", dir="/private/tmp") as directory:
        path = Path(directory) / "bridge.sock"
        received = []
        ready = threading.Event()

        def server():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                listener.listen(1)
                ready.set()
                connection, _ = listener.accept()
                with connection:
                    received.append(json.loads(connection.recv(65536)))
                    connection.sendall(b'{"ok":true}\n')

        thread = threading.Thread(target=server)
        thread.start()
        assert ready.wait(1)
        assert forward_event(normalize_event(_raw_event()), path, timeout=1) is True
        thread.join(timeout=1)
        assert received[0]["hook_event_name"] == "UserPromptSubmit"


def test_runtime_dir_does_not_depend_on_plugin_data():
    path = runtime_dir_from_env(
        {"PLUGIN_DATA": "/ignored/plugin/data", "AGENTSTACK_RUNTIME_DIR": "/runtime"}
    )
    assert path == type(path)("/runtime/codex-app")


def test_hook_entry_script_fails_open_and_spools_without_bridge(tmp_path):
    script = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "agentstack_codex_app"
        / "hook_entry.py"
    )
    runtime = tmp_path / "runtime"
    environment = os.environ.copy()
    environment["AGENTSTACK_CODEX_APP_RUNTIME_DIR"] = str(runtime)
    result = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(_raw_event()),
        text=True,
        capture_output=True,
        env=environment,
        timeout=2,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    spool = runtime / "hook-events.jsonl"
    assert spool.exists()
    assert "private prompt" not in spool.read_text(encoding="utf-8")
