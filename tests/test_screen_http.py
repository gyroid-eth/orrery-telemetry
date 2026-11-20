from contextlib import contextmanager
import http.client
import json
import threading
from urllib.parse import urlencode

import pytest

from dashboard import http_operator, server, screen_http
from hooks import screen_state


@contextmanager
def dashboard(monkeypatch, work, global_mode):
    home = work / "install"
    http_operator.configure(home)
    monkeypatch.setenv("AGENTSTACK_HOME", str(home))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(home / "runtime"))
    monkeypatch.setattr(server, "GLOBAL_MODE", global_mode)
    listener = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    monkeypatch.setenv("AGENTSTACK_PORT", str(listener.server_port))
    worker = threading.Thread(target=listener.serve_forever, daemon=True)
    worker.start()

    def request(method, path, token=None, body=None, headers=None):
        values = {"Content-Type": "application/json"}
        if token:
            values["Authorization"] = "Bearer " + token
        values.update(headers or {})
        conn = http.client.HTTPConnection(*listener.server_address, timeout=3)
        try:
            conn.request(method, path, json.dumps(body) if body is not None else None, values)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    try:
        yield request, http_operator.read_capability(home), screen_state.Store(home / "runtime")
    finally:
        listener.shutdown()
        listener.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize("global_mode", [False, True])
def test_child_cannot_change_generation_permissions(monkeypatch, tmp_path, global_mode):
    with dashboard(monkeypatch, tmp_path, global_mode) as (request, operator, store):
        project, agent = "sample-project", "SampleAgent"
        runtime, first, second = "1"*32, "2"*32, "3"*32
        target = dict(project=project, runtime=runtime, agent=agent, generation=first)
        token = store.launch(project, runtime, agent, first, "off")
        assert token not in str(request("GET", screen_http.OPERATOR, operator))
        assert request("GET", screen_http.CHILD, token) == (
            200, {"project": project, "runtime": runtime, "generation": first,
                  "computer_use": "off", "source": "default"},
        )
        body = {**target, "computer_use": "view"}
        assert request("POST", screen_http.OPERATOR, body=body)[0] == 403
        assert request("POST", screen_http.OPERATOR, token, body)[0] == 403
        assert request("POST", screen_http.OPERATOR, operator, body, {
            "Origin": "http://example.invalid",
        })[0] == 403
        assert request("POST", screen_http.OPERATOR, operator, {**body, "project": "other-project"})[0] == 404
        assert request("POST", screen_http.OPERATOR, operator, {**body, "runtime": "b"*32})[0] == 404
        assert request("POST", screen_http.OPERATOR, operator, {**body, "generation": second})[0] == 404
        assert request("POST", screen_http.OPERATOR, operator, body)[1]["source"] == "operator"
        assert request("GET", screen_http.CHILD, token)[1]["computer_use"] == "view"
        assert request("GET", screen_http.CHILD + "?" + urlencode(target), token)[0] == 400
        assert request("GET", screen_http.CHILD, operator)[0] == 403

        token_other = store.launch("other-project", runtime, agent, first, "off")
        assert request("GET", screen_http.CHILD, token_other)[1]["computer_use"] == "off"
        assert request("GET", screen_http.CHILD, token)[0] == 403
        new_token = store.launch(project, runtime, agent, second, "off")
        assert request("GET", screen_http.CHILD, token)[0] == 403
        assert request("GET", screen_http.CHILD, new_token)[1]["computer_use"] == "off"
        assert request("GET", screen_http.OPERATOR + "?" + urlencode(target), operator)[0] == 404
        store.revoke(target)
        assert request("GET", screen_http.CHILD, new_token)[0] == 200


def test_audit_scope_and_shape(monkeypatch, tmp_path):
    with dashboard(monkeypatch, tmp_path, False) as (request, operator, store):
        target = dict(project="sample-project", runtime="1"*32, agent="SampleAgent", generation="2"*32)
        token = store.launch(*target.values(), "control")
        other_target = {**target, "agent": "OtherAgent"}
        other = store.launch(*other_target.values(), "view")
        for i, outcome in enumerate(("ok", "denied", "upstream_error")):
            event = dict(call_id=str(i)*32, tool="get_window_state", phase="start", result=None)
            assert request("POST", screen_http.CHILD_EVENTS, token, {**event, "title": "secret"})[0] == 400
            assert request("POST", screen_http.CHILD_EVENTS, token, event)[0] == 200
            finish = {**event, "phase": "finish", "result": outcome}
            assert request("POST", screen_http.CHILD_EVENTS, other, finish)[0] == 409
            assert request("POST", screen_http.CHILD_EVENTS, token, finish)[0] == 200
        endpoint = screen_http.EVENTS + "?" + urlencode(target)
        assert request("GET", endpoint, token)[0] == 403
        rows = request("GET", endpoint, operator)[1]["calls"]
        assert {r["result"] for r in rows} == {"ok", "denied", "upstream_error"}
        assert all(set(r) == {"call_id", "agent", "tool", "started", "finished", "result"} for r in rows)
        assert all(r["agent"] == target["agent"] for r in rows)


@pytest.mark.asyncio
async def test_tool_calls_are_rechecked_via_real_stdio_and_http(monkeypatch, tmp_path):
    from datetime import timedelta
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from agentstack_mail.screen_mcp import SCHEMAS, driver_platform, public_schema
    from test_screen_mcp import FIXTURE

    monkeypatch.setenv("AGENTSTACK_SCREEN_PYTHON", sys.executable)
    platform = driver_platform()
    driver, log = tmp_path / "driver", tmp_path / "calls"
    fixture = FIXTURE.replace('SCHEMAS["darwin"]', f"SCHEMAS[{platform!r}]")
    driver.write_text("#!" + sys.executable + "\n" + fixture.replace(
        "log, scenario = sys.argv[1:]", f"log, scenario = {str(log)!r}, 'normal'",
    ))
    driver.chmod(0o700)
    monkeypatch.setenv("AGENTSTACK_CUA_DRIVER", str(driver))
    with dashboard(monkeypatch, tmp_path, False) as (request, operator, store):
        runtime = store.directory.parent
        definition = screen_state.definition(
            runtime, "sample-project", "SampleAgent", "1" * 32, "off",
        )
        async with stdio_client(StdioServerParameters(**definition)) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=8)) as mcp:
                assert (await mcp.initialize()).serverInfo.name == "orrery-screen"
                initial = (await mcp.list_tools()).tools
                assert {tool.name: tool.inputSchema for tool in initial} == {
                    tool: public_schema(value, tool) for tool, value in SCHEMAS[platform].items()
                }
                assert (await mcp.call_tool("check_permissions", {})).isError
                assert not log.exists()
                state = store.list()[0]
                state.pop("source")
                for mode, action_error in (("view", True), ("control", False), ("off", True)):
                    status, payload = request("POST", screen_http.OPERATOR, operator, {
                        **state, "computer_use": mode,
                    })
                    assert status == 200 and payload["computer_use"] == mode
                    assert (await mcp.list_tools()).tools == initial
                    assert (await mcp.call_tool("click", {"pid": 2})).isError is action_error
                assert log.read_text().splitlines() == ["started", '["click", {"pid": 2}]']
                assert request("POST", screen_http.OPERATOR, operator, {
                    **state, "computer_use": "control",
                })[0] == 200
                respond = screen_http.respond
                release = threading.Event()

                def delay_lookup(handler, path, *args):
                    if path == screen_http.CHILD:
                        release.wait(4)
                        handler.close_connection = True
                    else:
                        respond(handler, path, *args)

                monkeypatch.setattr(screen_http, "respond", delay_lookup)
                try:
                    delayed = await mcp.call_tool("click", {"pid": 2})
                    assert delayed.isError and "照会できません" in delayed.content[0].text
                finally:
                    release.set()
                    monkeypatch.setattr(screen_http, "respond", respond)
                assert len(log.read_text().splitlines()) == 2
                store.revoke({key: state[key] for key in screen_state.TARGET})
                assert (await mcp.call_tool("click", {"pid": 2})).isError
