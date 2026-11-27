"""The native client must call the tool. A tools/list reply cannot mark it ready."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "integrations/codex_app/src/agentstack_codex_app"
HOOK = ROOT / "hooks/child-mail-readiness.sh"
PROGRAMS = ("claude-code", "codex", "dsh", "hermes", "antigravity")
SUBJECT = "ORRERY child startup check"


@pytest.fixture
def mail_server():
    notices = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            call = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            name = call["params"]["name"]
            assert name == "send_message"
            notices.append(call["params"]["arguments"])
            result = {"structuredContent": {"deliveries": []}, "isError": False}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": call["id"], "result": result}).encode())

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/mcp", notices
        finally:
            server.shutdown()
            thread.join()


def tool_call(nonce, receiver="ChildAgent"):
    return {"method": "tools/call", "id": 2, "params": {"name": "send_message", "arguments": {
        "to": [receiver], "subject": SUBJECT, "body_md": nonce}}}


def proxy(env, *commands):
    completed = subprocess.run(
        [sys.executable, str(PACKAGE / "mcp_server.py")], env=env,
        input="".join(json.dumps({"jsonrpc": "2.0", **value}) + "\n" for value in commands),
        capture_output=True, text=True, timeout=15, check=True,
    )
    return [json.loads(line)["result"] for line in completed.stdout.splitlines()]


@pytest.mark.parametrize("program", PROGRAMS)
@pytest.mark.parametrize("registered", [True, False], ids=["callable", "cancelled-after-list"])
def test_startup_requires_callable_send_message(tmp_path, mail_server, program, registered):
    endpoint, notices = mail_server
    token = tmp_path / "token"
    token.write_text("fixture-owner-token")
    token.chmod(0o600)
    env = {**os.environ, "AGENTSTACK_PYTHON": sys.executable, "AGENTSTACK_RUNTIME_DIR": str(tmp_path),
           "AGENTSTACK_MCP_URL": endpoint, "AGENTSTACK_PROXY_TOKEN_FILE": str(token),
           "AGENTSTACK_PROJECT_KEY": "/fixture/project", "AGENTSTACK_PROXY_AGENT_NAME": "ChildAgent",
           "AGENTSTACK_PROXY_PROGRAM": program, "AGENTSTACK_PROXY_SERVER_NAME": "orrery-mail"}
    launcher_script = '''
        set -eu
        source "$1"
        CHILD_NAME=ChildAgent
        PROJECT_KEY=/fixture/project
        RUNTIME_DIR=$AGENTSTACK_RUNTIME_DIR
        MCP_URL=$AGENTSTACK_MCP_URL
        MAIL_ENV=$RUNTIME_DIR/no-mail-env
        HTTP_BEARER_MODE=disabled
        trap child_mail_end EXIT
        tmux() { return 0; }
        child_mail_begin ParentAgent "$AGENTSTACK_PROXY_PROGRAM" "$AGENTSTACK_PROXY_TOKEN_FILE"
        child_mail_prompt "Literal canonical task"
        printf '\\n'
        read -r unused
        CHILD_MAIL_DEADLINE=$SECONDS
        child_mail_wait ParentAgent "$AGENTSTACK_PROXY_TOKEN_FILE"
        echo ready
    '''
    with subprocess.Popen(["bash", "-c", launcher_script, "test", str(HOOK)], env=env,
                          text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as launch:
        try:
            first = launch.stdout.readline()
            assert first.startswith(f"{SUBJECT} for ChildAgent ") and "ORRERY Mail MCP send_message" in first
            assert launch.stdout.readline().strip() == ""
            assert launch.stdout.readline().strip() == "Literal canonical task"
            attempt, = tmp_path.glob(".mail-start-*")
            nonce = attempt.name.removeprefix(".mail-start-")
            assert nonce in first
            tools, = proxy(env, {"method": "tools/list", "id": 1})
            assert "send_message" in {tool["name"] for tool in tools["tools"]}
            assert not (attempt / "verdict").exists()
            if registered:
                alias, = proxy({**env, "AGENTSTACK_PROXY_SERVER_NAME": "agentstack"}, tool_call(nonce))
                assert alias["isError"] and not (attempt / "verdict").exists()
                # Wrong receiver and a stale nonce must have no side effects.
                replies = proxy(env, tool_call(nonce, "ParentAgent"), tool_call("0" * 32),
                                tool_call(nonce), tool_call(nonce))
                assert [r["isError"] for r in replies] == [True, True, False, False]
                assert replies[-1]["structuredContent"] == {"startup_confirmed": True}
                assert (attempt / "verdict").read_text() == "ready\n"
            out, err = launch.communicate("continue\n", timeout=15)
        finally:
            if launch.poll() is None:
                launch.kill()
                launch.wait(timeout=5)
    assert not attempt.exists()
    if registered:
        assert launch.returncode == 0, err
        assert out.strip() == "ready"
        assert notices == []
        stale, = proxy(env, tool_call(nonce))
        assert stale["isError"] and notices == []
    else:
        assert launch.returncode != 0
        assert out == ""
        assert "mail startup failure: ChildAgent:" in err
        assert "did not confirm orrery-mail send_message" in err
        notice, = notices
        assert notice["sender_name"] == "ChildAgent" and notice["to"] == ["ParentAgent"]
        assert "did not confirm orrery-mail send_message" in notice["body_md"]
