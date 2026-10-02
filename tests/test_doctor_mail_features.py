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


def _doctor(tmp_path: pathlib.Path, url: str, running: str = "abc1234",
            repo: pathlib.Path | None = None) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    service = home / ".agentstack" / "mail-service"
    render = service / "renders" / f"{running}-0001"
    render.mkdir(parents=True)
    (render / "deployment.json").write_text(json.dumps({"source_id": running}))
    if repo is not None:
        (home / ".agentstack" / "install-state.json").write_text(json.dumps({"repo_root": str(repo)}))
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
        # Without the install's checkout the direction is unknown: the lack is
        # reported, but no update from some checkout is suggested.
        assert FEATURES[0]["needed_for"] in result.stderr
        assert "--mail update" not in result.stderr
        assert "at least as new as the running build" in result.stderr
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


def test_the_stale_notice_says_what_breaks_how_to_update_the_risk_and_the_recovery(history):
    repo, old, new, side = history
    text = _notice("stale", "--repo", str(repo), "--running", old, "--to", new,
                   "--missing", "register_agent.existing_agent_id")
    assert text.splitlines()[0] == f"ORRERY Mail is out of date (running {old[:7]}, this checkout {new[:7]})."
    assert FEATURES[0]["needed_for"] in text and FEATURES[0]["without_it"] in text
    assert "./scripts/install.sh --mail update" in text and "./scripts/setup.sh --mail update" in text
    assert "Children and Codex reconnect by themselves" in text
    assert "18 s and 19 s did not" in text
    # The Claude Code screens as they are: /mcp, the failed server, Reconnect.
    assert "/mcp, choose orrery-mail (shown as \u2718 failed), then Reconnect (not Authenticate)" in text
    assert len(text.splitlines()) <= 10


def test_without_how_leaves_the_update_command_to_the_caller(history):
    repo, old, new, side = history
    text = _notice("stale", "--repo", str(repo), "--running", old, "--to", new,
                   "--missing", "register_agent.existing_agent_id", "--without-how")
    assert "--mail update" not in text
    assert text.startswith("ORRERY Mail is out of date") and "Reconnect" in text


def test_the_after_stop_notice_is_the_same_recovery():
    text = _notice("after-stop", "--outage", "3.2")
    assert text.startswith("ORRERY Mail was unavailable for 3.2s.")
    assert "then Reconnect (not Authenticate)" in text and "check before repeating it" in text


def test_an_unreachable_mail_proves_nothing_either_way():
    # Unreachable: neither "complete" nor "out of date"; only what is known.
    text = _notice("stale", "--mcp-url", "http://127.0.0.1:9/mcp", "--running", "build-a", "--to", "build-b")
    assert "out of date" not in text and "cannot be told" in text
    assert _notice("stale", "--mcp-url", "http://127.0.0.1:9/mcp", "--running", "build-a",
                   "--to", "build-b", "--only-if-stale") == ""


@pytest.fixture
def history(tmp_path):
    """A checkout with an older build, a newer one and a diverged one."""
    repo = tmp_path / "repo"
    package = repo / "packages" / "agentstack_mail"
    package.mkdir(parents=True)

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True,
                              capture_output=True, env={**os.environ, "GIT_AUTHOR_NAME": "t",
                              "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
                              "GIT_COMMITTER_EMAIL": "t@example.invalid"}).stdout.strip()

    def commit(text):
        (package / "app.py").write_text(text)
        git("add", "-A")
        git("commit", "-q", "-m", text)
        return git("rev-parse", "HEAD")

    git("init", "-q", "-b", "main")
    old = commit("old")
    new = commit("new")
    git("switch", "-q", "-c", "side", old)
    side = commit("side")
    git("switch", "-q", "main")
    return repo, old, new, side


def _stale(repo, running, to, *extra):
    return _notice("stale", "--repo", str(repo), "--running", running, "--to", to, "--missing", "", *extra)


def test_only_an_older_running_mail_is_called_out_of_date(history):
    repo, old, new, side = history
    older = _stale(repo, old, new)
    assert older.startswith("ORRERY Mail is out of date") and "--mail update" in older
    # Newer than the checkout: updating from it would go back. Say so instead.
    newer = _stale(repo, new, old)
    assert "out of date" not in newer and "--mail update" not in newer
    assert "newer than this checkout" in newer
    # Diverged or not a commit of this checkout: which is newer cannot be told.
    for running in (side, "fixture-build"):
        text = _stale(repo, running, new)
        assert "out of date" not in text and "--mail update" not in text
        assert "cannot be told" in text


def test_only_if_stale_prints_nothing_unless_the_running_mail_is_older(history):
    repo, old, new, side = history
    assert _stale(repo, old, new, "--only-if-stale").startswith("ORRERY Mail is out of date")
    for running in (new, side, "fixture-build"):
        assert _stale(repo, running, old if running == new else new, "--only-if-stale") == ""


@pytest.mark.parametrize("which", ["older", "diverged", "unknown"])
def test_a_missing_feature_is_reported_but_only_an_older_mail_gets_the_update_command(history, which):
    repo, old, new, side = history
    running = {"older": old, "diverged": side, "unknown": "fixture-build"}[which]
    text = _notice("stale", "--repo", str(repo), "--running", running, "--to", new,
                   "--missing", "register_agent.existing_agent_id")
    assert FEATURES[0]["needed_for"] in text
    if which == "older":
        assert text.startswith("ORRERY Mail is out of date") and "--mail update" in text
    else:
        assert "out of date" not in text and "--mail update" not in text
        assert "at least as new as the running build" in text
    # Every notice that urges an update carries the shared risk and recovery.
    assert "no short limit is guaranteed" in text and "then Reconnect (not Authenticate)" in text


def test_a_missing_feature_without_a_checkout_does_not_suggest_an_update():
    text = _notice("stale", "--running", "abc", "--missing", "register_agent.existing_agent_id")
    assert "lacks features this install relies on" in text and "--mail update" not in text
    assert "no short limit is guaranteed" in text and "then Reconnect (not Authenticate)" in text


def test_the_risk_does_not_promise_a_short_stop_and_states_observations_as_such(history):
    repo, old, new, side = history
    text = _stale(repo, old, new)
    assert "usually a few seconds" in text
    assert "no short limit is guaranteed" in text
    assert "Claude Code 2.1.287" in text and "18 s and 19 s" in text and "8 s and 15 s" in text


def test_a_missing_feature_on_a_newer_running_mail_never_suggests_updating_from_this_checkout(history):
    """Missing proves Mail lacks a feature, not that this older checkout would add it."""
    repo, old, new, side = history
    text = _notice("stale", "--repo", str(repo), "--running", new, "--to", old,
                   "--missing", "register_agent.existing_agent_id")
    assert FEATURES[0]["needed_for"] in text
    assert "--mail update" not in text and "out of date" not in text
    assert "newer than this checkout" in text and "Pull the checkout" in text
    assert _notice("stale", "--repo", str(repo), "--running", new, "--to", old,
                   "--missing", "register_agent.existing_agent_id", "--only-if-stale") == ""


@pytest.mark.parametrize("direction", ["older", "newer"])
def test_doctor_suggests_updating_only_from_a_checkout_newer_than_the_running_mail(tmp_path, history, direction):
    repo, old, new, side = history
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", new if direction == "older" else old], check=True)
    running = old if direction == "older" else new
    server = _server({"name"})
    try:
        result = _doctor(tmp_path, f"http://127.0.0.1:{server.server_port}/mcp", running=running, repo=repo)
    finally:
        server.shutdown()
    assert FEATURES[0]["needed_for"] in result.stderr
    if direction == "older":
        assert "./scripts/install.sh --mail update" in result.stderr
        assert "then Reconnect (not Authenticate)" in result.stderr
    else:
        assert "--mail update" not in result.stderr and "Pull the checkout" in result.stderr
