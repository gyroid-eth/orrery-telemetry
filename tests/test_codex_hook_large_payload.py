"""A Codex hook must not fail on a large payload (162nd seminar, exit 141).

run-hook.sh pipes the payload into Python under `set -o pipefail`. When the
Python side returned without reading all of stdin -- hook_entry.py stops at
its 64 KiB event limit, the session-index recorder reads nothing without a
launch binding -- the `printf` writing the rest died of SIGPIPE and the hook
exited 141 ("Hook failed" in Codex, several times while digest-paper ran),
and the recorder pipe printed a false `recorder_process_failed`.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_HOOK = ROOT / "integrations" / "codex_app" / "plugin" / "scripts" / "run-hook.sh"


def _run(tmp_path: pathlib.Path, size: int, *, binding: bool) -> subprocess.CompletedProcess[bytes]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_CODEX_APP_INSTALL_DIR": str(tmp_path / "no-install"),
    }
    if binding:
        env["AGENTSTACK_CODEX_LAUNCH_BINDING"] = str(tmp_path / "binding.json")
        env["AGENTSTACK_CODEX_LAUNCH_ID"] = "fixture-launch"
    payload = json.dumps({"hook_event_name": "PostToolUse", "session_id": "s",
                          "tool_response": "x" * size}).encode()
    return subprocess.run(["bash", str(RUN_HOOK)], input=payload, env=env,
                          capture_output=True, check=False, timeout=60)


@pytest.mark.parametrize("binding", [False, True])
@pytest.mark.parametrize("size", [1_000, 100_000, 300_000, 2_000_000])
def test_any_payload_size_exits_zero_without_a_false_warning(tmp_path, size, binding):
    done = _run(tmp_path, size, binding=binding)
    assert done.returncode == 0, (size, done.returncode, done.stderr[-300:])
    assert b"recorder_process_failed" not in done.stderr
