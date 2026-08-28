"""Verify the public contract from an installed agentstack-mail wheel."""

from __future__ import annotations

import asyncio
import json
from importlib.resources import files
from typing import Any

from agentstack_mail.app import build_mcp_server
from agentstack_mail.authorization import (
    assert_authorization_catalog_boundary,
    catalog_as_plain_data,
)
from agentstack_mail.contract import COMPATIBILITY_TOOLS


async def verify() -> None:
    authorization_fixture = json.loads(
        files("agentstack_mail")
        .joinpath("fixtures/authorization-tools-v1.json")
        .read_text(encoding="utf-8")
    )
    fixture = json.loads(
        files("agentstack_mail")
        .joinpath("fixtures/live-tools-list.json")
        .read_text(encoding="utf-8")
    )
    schema_fields = ("inputSchema", "outputSchema", "_meta")
    expected = {
        tool["name"]: {key: tool[key] for key in schema_fields if key in tool}
        for tool in fixture["tools"]
        if tool["name"] in COMPATIBILITY_TOOLS
    }
    expected_read_tool_descriptions = {
        tool["name"]: tool["description"]
        for tool in fixture["tools"]
        if tool["name"] in {"search_messages", "summarize_thread"}
    }

    server = build_mcp_server()
    tools = await server.get_tools()
    resources = await server.get_resources()
    resource_templates = await server.get_resource_templates()
    prompts = await server.get_prompts()
    assert_authorization_catalog_boundary(tools)
    if authorization_fixture.get("tools") != catalog_as_plain_data():
        raise SystemExit("installed wheel authorization table mismatch")
    actual: dict[str, dict[str, Any]] = {}
    for name, tool in tools.items():
        dumped = tool.to_mcp_tool().model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
        actual[name] = {key: dumped[key] for key in schema_fields if key in dumped}

    if set(tools) != COMPATIBILITY_TOOLS or len(tools) != len(COMPATIBILITY_TOOLS):
        raise SystemExit(
            "installed wheel tool boundary mismatch: "
            f"missing={sorted(COMPATIBILITY_TOOLS - set(tools))}, "
            f"extra={sorted(set(tools) - COMPATIBILITY_TOOLS)}"
        )
    if resources:
        raise SystemExit("installed wheel must publish zero concrete MCP resources")
    if resource_templates:
        raise SystemExit("installed wheel must publish zero MCP resource templates")
    if prompts:
        raise SystemExit("installed wheel must publish zero MCP prompts")
    if actual != expected:
        mismatched = sorted(
            name for name in expected if actual.get(name) != expected[name]
        )
        raise SystemExit("installed wheel schema mismatch: " + ", ".join(mismatched))
    actual_read_tool_descriptions = {
        name: tools[name].description for name in expected_read_tool_descriptions
    }
    if actual_read_tool_descriptions != expected_read_tool_descriptions:
        raise SystemExit("installed wheel read-tool description mismatch")
    if any("resource://agents" in (tool.description or "") for tool in tools.values()):
        raise SystemExit("installed wheel advertises a suppressed roster resource")


if __name__ == "__main__":
    asyncio.run(verify())
