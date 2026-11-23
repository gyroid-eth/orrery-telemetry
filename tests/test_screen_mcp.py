import asyncio
import base64
import io
import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from PIL import Image

from agentstack_mail import screen_contract as contract
from agentstack_mail.screen_mcp import (
    MAX_BYTES, MAX_TEXT_BYTES, Relay, SCHEMAS, ScreenServer, TRUNCATED,
    bound_result, driver_parameters, public_schema,
)


FIXTURE = """\
import asyncio
import json
import os
from pathlib import Path
import sys
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from agentstack_mail.screen_mcp import SCHEMAS

log, scenario = sys.argv[1:]
server = Server("cua-driver", version="0.55.0")
with open(log, "a") as out:
    out.write("started\\n")
Path(log + ".pid").write_text(str(os.getpid()))

@server.list_tools()
async def tools():
    definitions = SCHEMAS["darwin"]
    result = [types.Tool(name=name, **definition) for name, definition in definitions.items()]
    if scenario == "schema":
        result[0].inputSchema = {"type": "object"}
    return result

@server.call_tool()
async def call(name, arguments):
    with open(log, "a") as out:
        out.write(json.dumps([name, arguments])+"\\n")
    if scenario == "hang":
        await asyncio.sleep(60)
    if scenario == "exit":
        raise SystemExit(5)
    structured = {"status": "ok"}
    if name == "get_window_state":
        structured = {"pid": 2, "window_id": 3, "window_title": "fixture title",
                      "tree_markdown": "- [27] Button Synthetic", "snapshot_id": "s00000001"}
    return types.CallToolResult(content=[types.TextContent(type="text", text="fixture")],
                                structuredContent=structured)

async def run():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

asyncio.run(run())
"""


class Authority:
    mode = "off"
    unavailable = False

    def __init__(self):
        self.events = []

    async def status(self):
        if self.unavailable:
            from agentstack_mail.screen_mcp import ScreenError
            raise ScreenError("fixture unavailable")
        return {"computer_use": self.mode}

    async def event(self, *event):
        self.events.append(event)


@pytest.fixture
def upstream(tmp_path):
    driver = tmp_path / "driver.py"
    driver.write_text(FIXTURE)
    log = tmp_path / "calls"

    def config(scenario):
        return StdioServerParameters(command=sys.executable, args=[str(driver), str(log), scenario])

    return config, log


def test_fixed_list_and_live_permissions(upstream):
    asyncio.run(_fixed_list_and_live_permissions(upstream))


async def _fixed_list_and_live_permissions(upstream):
    config, log = upstream
    authority = Authority()
    relay = Relay(SCHEMAS["darwin"], authority, lambda: config("normal"), timeout=4)
    server = ScreenServer(relay)

    async with server._lifespan_manager():
        async def listing():
            result = await server._list_tools_mcp()
            return [tool.model_dump(exclude_none=True) for tool in result]

        fixed = await listing()
        assert {t["name"] for t in fixed} == set(contract.TOOLS)
        assert (await server._call_tool_mcp("typo", {})).isError
        assert (await server._call_tool_mcp("check_permissions", {})).isError
        assert not log.exists()

        authority.mode = "view"
        assert await listing() == fixed
        assert (await server._call_tool_mcp("click", {"pid": 2})).isError
        assert (await server._call_tool_mcp("check_permissions", {"prompt": True})).isError
        assert (await server._call_tool_mcp("get_window_state", {
            "pid": 2, "window_id": 3, "screenshot_out_file": "/tmp/fixture.png",
        })).isError
        assert not log.exists()
        read = await server._call_tool_mcp("get_window_state", {"pid": 2, "window_id": 3})
        assert not read.isError
        assert read.structuredContent["window_title"] == "fixture title"

        authority.mode = "control"
        assert await listing() == fixed
        assert not (await server._call_tool_mcp("click", {"pid": 2})).isError
        authority.mode = "off"
        assert (await server._call_tool_mcp("check_permissions", {})).isError
        authority.mode, authority.unavailable = "control", True
        assert (await server._call_tool_mcp("click", {"pid": 2})).isError

    started, first, clicked = log.read_text().splitlines()
    assert started == "started"
    assert json.loads(first) == ["get_window_state", {
        "pid": 2, "window_id": 3, "full_output": False,
        "max_depth": 25, "max_elements": 250, "max_image_dimension": 1280,
    }]
    assert json.loads(clicked) == ["click", {"pid": 2}]
    assert len(authority.events) == 18
    for start, finish in zip(authority.events[::2], authority.events[1::2]):
        assert start[:2] == finish[:2]
        assert start[2] == "start" and finish[2] == "finish"
        assert finish[3] in {"ok", "denied"}
    assert "fixture title" not in repr(authority.events)


@pytest.mark.parametrize("scenario", ["schema", "hang", "exit"])
def test_relay_stops_and_never_replays(upstream, scenario):
    asyncio.run(_relay_stops_and_never_replays(upstream, scenario))


async def _relay_stops_and_never_replays(upstream, scenario):
    config, log = upstream
    authority = Authority()
    authority.mode = "control"
    # The same limit also covers starting the fixture driver (Python and the
    # mcp import). On a slow runner that alone took over 2 seconds, so the
    # click never reached the driver. Only "hang" waits for the full limit.
    relay = Relay(SCHEMAS["darwin"], authority, lambda: config(scenario), timeout=10)
    server = ScreenServer(relay)
    async with server._lifespan_manager():
        assert (await server._call_tool_mcp("click", {"pid": 2})).isError
        assert (await server._call_tool_mcp("click", {"pid": 2})).isError
    assert log.read_text().splitlines() == {
        "schema": ["started"],
        "hang": ["started", '["click", {"pid": 2}]'],
        "exit": ["started", '["click", {"pid": 2}]'],
    }[scenario]
    assert authority.events[-1][-1] == "upstream_error"
    with pytest.raises(ProcessLookupError):
        os.kill(int(log.with_suffix(".pid").read_text()), 0)


def test_bound_tree_and_pixel_coordinates():
    def png(width, height):
        data = io.BytesIO()
        Image.new("RGB", (width, height)).save(data, format="PNG")
        return types.ImageContent(type="image", mimeType="image/png",
                                  data=base64.b64encode(data.getvalue()).decode())

    tree = {"pid": 2, "window_id": 3, "capture_id": "sample",
            "elements": [{"element_index": i + 27, "role": "Button", "depth": 0,
                          "label": "観" * 1000} for i in range(200)]}
    result = bound_result(types.CallToolResult(content=[
        png(1450, 200), png(1280, 900), png(1100, 1000),
        types.TextContent(type="text", text=json.dumps(tree, ensure_ascii=False)),
    ], structuredContent=tree))

    assert len(result.model_dump_json().encode()) < MAX_BYTES
    limited = result.structuredContent
    assert len(json.dumps(limited, ensure_ascii=False).encode()) <= MAX_TEXT_BYTES + 2000
    assert limited["capture_id"] == "sample"
    assert limited["elements"] == tree["elements"][:len(limited["elements"])]
    assert 0 < len(limited["elements"]) < len(tree["elements"])
    assert [block.data for block in result.content if isinstance(block, types.ImageContent)] == [png(1280, 900).data]
    assert result.content[-1].text == TRUNCATED


@pytest.mark.parametrize(("platform", "binary", "args"), [
    ("darwin", "cua-driver", ["mcp"]),
    ("linux", "cua-driver", ["mcp"]),
    ("windows", "cua-driver.exe", ["mcp", "--socket", r"\\.\pipe\cua-driver"]),
])
def test_pinned_platform_tool_definitions_and_command(monkeypatch, platform, binary, args):
    monkeypatch.setenv("AGENTSTACK_CUA_DRIVER", "/fixture/" + binary)
    monkeypatch.setenv("AGENTSTACK_SCREEN_BINDING_FILE", "/fixture/private")
    monkeypatch.setenv("WSL_INTEROP", "/fixture/interop")
    params = driver_parameters(platform)
    assert params.command == "/fixture/" + binary
    assert params.args == args and params.env["WSL_INTEROP"] == "/fixture/interop"
    assert "AGENTSTACK_SCREEN_BINDING_FILE" not in params.env
    definitions = SCHEMAS[platform]
    assert set(definitions) == set(contract.TOOLS)
    read = public_schema(definitions["get_window_state"], "get_window_state")
    assert "screenshot_out_file" not in read["properties"]
    assert read["properties"]["max_image_dimension"]["maximum"] == 1280
    check = public_schema(definitions["check_permissions"], "check_permissions")
    assert check["properties"]["prompt"]["const"] is False
