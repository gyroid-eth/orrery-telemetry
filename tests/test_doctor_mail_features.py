"""agentstack-doctor reports what the running ORRERY Mail cannot do yet.

A Mail older than the install answers health and passes every other check,
while a feature the dashboard or a launcher relies on is missing: on
2026-10-02 a Claude resume fell back to a conversation without Mail because
register_agent had no existing_agent_id. doctor reads the live tools/list,
warns for each missing feature, keeps its exit status (Mail itself works),
and ends the section with a `mail-features:` line that setup.sh reads.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCTOR = ROOT / "scripts" / "doctor.sh"
FEATURES = json.loads((ROOT / "scripts" / "lib" / "mail_required_features.json").read_text())["features"]


def _server(register_params: set[str]):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if body["method"] == "tools/list":
                result = {"tools": [{"name": "register_agent", "inputSchema": {
                    "properties": {name: {} for name in register_params}}}]}
            else:
                result = {"structuredContent": {"status": "ok", "database_url": "sqlite:///nowhere"}}
            payload = json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _doctor(tmp_path: pathlib.Path, url: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    service = home / ".agentstack" / "mail-service"
    render = service / "renders" / "abc1234-0001"
    render.mkdir(parents=True)
    (render / "deployment.json").write_text(json.dumps({"source_id": "abc1234"}))
    (service / "runtime").mkdir(parents=True)
    (service / "runtime" / "agentstack-mail.pid").write_text(f"1\n{render}/run-agentstack-mail.sh\n")
    return subprocess.run(
        ["bash", str(DOCTOR), "--install-dir", str(home / ".agentstack")],
        env={**os.environ, "HOME": str(home), "AGENTSTACK_MAIL_DIR": str(service),
             "AGENTSTACK_MCP_URL": url, "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled"},
        text=True, capture_output=True, check=False, timeout=120,
    )


def _features_line(stdout: str) -> str:
    (line,) = [line for line in stdout.splitlines() if line.startswith("mail-features: ")]
    return line


def test_the_list_names_what_the_dashboard_checks():
    assert {"tool": "register_agent", "parameter": "existing_agent_id"}.items() <= FEATURES[0].items()
    for feature in FEATURES:
        assert set(feature) == {"tool", "parameter", "needed_for", "without_it"}
        assert " " not in feature["tool"] + feature["parameter"]


@pytest.mark.parametrize("params,status,missing", [
    ({"name", "registration_token"}, "missing", "register_agent.existing_agent_id"),
    ({"name", "registration_token", "existing_agent_id"}, "ok", ""),
])
def test_a_missing_feature_is_a_warning_with_a_fix_not_a_failure(tmp_path, params, status, missing):
    server = _server(params)
    try:
        result = _doctor(tmp_path, f"http://127.0.0.1:{server.server_port}/mcp")
    finally:
        server.shutdown()
    assert _features_line(result.stdout) == (
        f"mail-features: status={status} missing={missing} running=abc1234")
    if status == "missing":
        assert ("warn: ORRERY Mail (running abc1234) lacks register_agent.existing_agent_id"
                in result.stderr)
        # The shared notice: how to update, the risk, and the /mcp recovery.
        assert "./scripts/install.sh --mail update" in result.stderr
        assert "then Reconnect (not Authenticate)" in result.stderr
    else:
        assert "ORRERY Mail has every feature this install relies on" in result.stdout
        assert "lacks" not in result.stderr


def test_the_exit_status_does_not_depend_on_features(tmp_path):
    codes = []
    for params in ({"name"}, {"name", "existing_agent_id"}):
        server = _server(params)
        try:
            codes.append(_doctor(tmp_path / str(len(codes)), f"http://127.0.0.1:{server.server_port}/mcp").returncode)
        finally:
            server.shutdown()
    assert codes[0] == codes[1]


def test_an_unreachable_mail_is_unknown_not_ok(tmp_path):
    result = _doctor(tmp_path, "http://127.0.0.1:9/mcp")
    assert _features_line(result.stdout) == "mail-features: status=unknown missing= running=abc1234"
    assert "its features were not checked" in result.stderr


NOTICE = ROOT / "scripts" / "lib" / "mail_update_notice.py"


def _notice(*args: str) -> str:
    done = subprocess.run([sys.executable, str(NOTICE), *args], text=True, capture_output=True, check=True)
    return done.stdout


def test_the_stale_notice_says_what_breaks_how_to_update_the_risk_and_the_recovery():
    text = _notice("stale", "--running", "52d76241ffe9", "--to", "e00cd38aaaa00",
                   "--missing", "register_agent.existing_agent_id")
    assert text.splitlines()[0] == "ORRERY Mail is out of date (running 52d7624, this checkout e00cd38)."
    assert FEATURES[0]["needed_for"] in text and FEATURES[0]["without_it"] in text
    assert "./scripts/install.sh --mail update" in text and "./scripts/setup.sh --mail update" in text
    assert "Children and Codex reconnect by themselves" in text
    assert "18 s or more" in text
    # The Claude Code screens as they are: /mcp, the failed server, Reconnect.
    assert "/mcp, choose orrery-mail (shown as \u2718 failed), then Reconnect (not Authenticate)" in text
    assert len(text.splitlines()) <= 10


def test_without_how_leaves_the_update_command_to_the_caller():
    text = _notice("stale", "--missing", "", "--without-how")
    assert "--mail update" not in text
    assert "have not reached you yet" in text and "Reconnect" in text


def test_the_after_stop_notice_is_the_same_recovery():
    text = _notice("after-stop", "--outage", "3.2")
    assert text.startswith("ORRERY Mail was unavailable for 3.2s.")
    assert "then Reconnect (not Authenticate)" in text and "check before repeating it" in text


def test_an_unreachable_mail_is_not_reported_as_complete():
    text = _notice("stale", "--mcp-url", "http://127.0.0.1:9/mcp")
    assert "have not reached you yet" in text
