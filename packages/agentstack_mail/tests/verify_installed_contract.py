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
    expected = {
        tool["name"]: {
            "inputSchema": tool["inputSchema"],
            "outputSchema": tool["outputSchema"],
            "_meta": tool["_meta"],
        }
        for tool in fixture["tools"]
        if tool["name"] in COMPATIBILITY_TOOLS
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
        actual[name] = {
            "inputSchema": dumped["inputSchema"],
            "outputSchema": dumped["outputSchema"],
            "_meta": dumped["_meta"],
        }

    if set(tools) != COMPATIBILITY_TOOLS or len(tools) != 22:
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
    if any("resource://agents" in (tool.description or "") for tool in tools.values()):
        raise SystemExit("installed wheel advertises a suppressed roster resource")


if __name__ == "__main__":
    asyncio.run(verify())
