"""`agentstack-mailctl` must be able to manage a launchd-supervised server.

Reported by a tester on 2026-08-17: `status`, `stop` and `restart` all died with
"endpoint is occupied without a live managed pid" against a healthy server. The
plist runs `agentstack-mail-service foreground ...` directly, while the
controller only recognises a process started through `run-agentstack-mail.sh`,
so its ownership guard could never match the launchd instance and the documented
CLI was unusable -- operators had to fall back to raw launchctl.

The controller now defers to launchd where launchd is the supervisor. Signalling
launchd's child behind its back is what made "who supervises this" ambiguous in
the first place.
"""

from __future__ import annotations

import http.server
import json
import subprocess
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAILCTL = REPO_ROOT / "bin" / "agentstack-mailctl"
LABEL = "org.orrery.mail"

FAKE_LAUNCHCTL = """#!/bin/bash
echo "$@" >> "$LAUNCHCTL_LOG"
case "$1" in
  print)
    if [[ -f "$LOADED_MARKER" ]]; then
      echo "	pid = 4242"
      exit 0
    fi
    exit 113
    ;;
  bootout)
    rm -f "$LOADED_MARKER" "$SERVING_MARKER"
    exit 0
    ;;
  kickstart)
    touch "$SERVING_MARKER"
    exit 0
    ;;
esac
exit 0
"""


class _MailHandler(http.server.BaseHTTPRequestHandler):
    serving_marker: Path

    database_path: Path

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if not self.serving_marker.exists():
            self.send_response(503)
            self.end_headers()
            return
        # health_ok checks the reported database, not just the status.
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "structuredContent": {
                    "status": "ok",
                    "database_url": f"sqlite+aiosqlite:///{self.database_path}",
                }
            },
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self.send_response(406 if self.serving_marker.exists() else 503)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture()
def harness(tmp_path: Path):
    serving = tmp_path / "serving"
    serving.touch()
    _MailHandler.serving_marker = serving
    _MailHandler.database_path = tmp_path / "storage.sqlite3"
    server = http.server.HTTPServer(("127.0.0.1", 0), _MailHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    launchctl = fakebin / "launchctl"
    launchctl.write_text(FAKE_LAUNCHCTL, encoding="utf-8")
    launchctl.chmod(0o755)

    loaded = tmp_path / "loaded"
    loaded.touch()
    log = tmp_path / "launchctl.log"
    host, port = server.server_address[:2]

    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path / "home"),
        "LAUNCHCTL_LOG": str(log),
        "LOADED_MARKER": str(loaded),
        "SERVING_MARKER": str(serving),
        "AGENTSTACK_MAILCTL_SKIP_ENV": "1",
        "AGENTSTACK_MAIL_PROVIDER": "agentstack",
        "AGENTSTACK_MAIL_ENV": str(tmp_path / "service" / "env"),
        "AGENTSTACK_MAIL_DB": str(tmp_path / "storage.sqlite3"),
        "AGENTSTACK_MAIL_RUNTIME_DIR": str(tmp_path / "runtime"),
        "AGENTSTACK_MCP_URL": f"http://{host}:{port}/mcp",
        "AGENTSTACK_MAIL_LAUNCHD_LABEL": LABEL,
    }
    (tmp_path / "home").mkdir()
    (tmp_path / "service").mkdir()
    (tmp_path / "service" / "env").write_text("", encoding="utf-8")
    (tmp_path / "storage.sqlite3").write_text("", encoding="utf-8")
    try:
        yield env, loaded, serving, log
    finally:
        server.shutdown()
        server.server_close()


def _mailctl(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(MAILCTL), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_status_recognises_the_launchd_supervised_server(harness) -> None:
    env, _loaded, _serving, _log = harness
    result = _mailctl(env, "status")
    assert result.returncode == 0, result.stderr
    assert "running under launchd" in result.stdout
    assert LABEL in result.stdout
    assert "occupied without a live managed pid" not in result.stderr


def test_stop_asks_launchd_instead_of_signalling_its_child(harness) -> None:
    env, _loaded, _serving, log = harness
    result = _mailctl(env, "stop")
    assert result.returncode == 0, result.stderr
    assert f"bootout gui/" in log.read_text()
    assert LABEL in log.read_text()
    assert "stopped" in result.stdout


def test_restart_is_one_launchctl_call(harness) -> None:
    env, _loaded, _serving, log = harness
    result = _mailctl(env, "restart")
    assert result.returncode == 0, result.stderr
    calls = [line for line in log.read_text().splitlines() if line.startswith("kickstart")]
    assert any("-k" in call for call in calls), calls
    # A bootout would leave the job unloaded if the start half failed.
    assert "bootout" not in log.read_text()


def test_start_does_not_add_a_second_server_next_to_launchds(harness) -> None:
    env, _loaded, _serving, log = harness
    result = _mailctl(env, "start")
    assert result.returncode == 0, result.stderr
    assert "already running under launchd" in result.stdout
    assert "run-agentstack-mail" not in log.read_text()


def test_without_launchd_the_controller_still_refuses_a_foreign_endpoint(
    harness,
) -> None:
    """The null case: the deference must not become a blanket 'assume it's fine'.

    With no launchd job loaded, an occupied endpoint is still someone else's
    process and the controller must say so rather than claim ownership.
    """
    env, loaded, _serving, _log = harness
    loaded.unlink()
    result = _mailctl(env, "status")
    assert result.returncode != 0
    assert "occupied without a live managed pid" in result.stderr
