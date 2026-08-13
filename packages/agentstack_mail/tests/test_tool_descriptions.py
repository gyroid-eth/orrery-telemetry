"""Published tool descriptions must stay compact.

tools/list once weighed 38 KB (~9,500 tokens) because the upstream-derived
docstrings were published verbatim, and every agent session paid that before
its first message. These tests pin the substitution seam in the boundary and
a hard size budget so a future tool (or a docstring edit) cannot quietly
re-inflate the surface.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agentstack_mail.app import build_mcp_server
from agentstack_mail.contract import COMPATIBILITY_TOOLS
from agentstack_mail.tool_descriptions import COMPACT_TOOL_DESCRIPTIONS

# Frozen by live-tools-list.json (test_actual_tool_schemas_match_the_frozen
# _live_contract); their descriptions must NOT be in the compact table.
FROZEN_DESCRIPTION_TOOLS = {"search_messages", "summarize_thread"}



def _dump_tools() -> list[dict[str, Any]]:
    async def inspect() -> list[dict[str, Any]]:
        tools = await build_mcp_server().get_tools()
        return [
            tool.to_mcp_tool().model_dump(mode="json", by_alias=True, exclude_none=True)
            for tool in tools.values()
        ]

    return asyncio.run(inspect())


def test_compact_table_covers_exactly_the_replaceable_tools() -> None:
    replaceable = COMPATIBILITY_TOOLS - FROZEN_DESCRIPTION_TOOLS
    assert set(COMPACT_TOOL_DESCRIPTIONS) == replaceable


def test_published_descriptions_come_from_the_compact_table() -> None:
    published = {t["name"]: t.get("description", "") for t in _dump_tools()}
    assert set(published) == COMPATIBILITY_TOOLS
    for name, description in published.items():
        if name in FROZEN_DESCRIPTION_TOOLS:
            continue
        assert description == COMPACT_TOOL_DESCRIPTIONS[name], name


def test_tools_list_stays_inside_the_token_budget() -> None:
    dumped = _dump_tools()
    descriptions = sum(len(t.get("description", "")) for t in dumped)
    total = len(json.dumps({"tools": dumped}))
    # Descriptions were 22,285 B before the compact table and 5,615 B after.
    # The budget leaves headroom for new tools without letting the old
    # docstring-publishing behavior sneak back (one regression re-adds ~1 KB
    # per tool, which blows through this immediately).
    assert descriptions < 8_000, f"published descriptions grew to {descriptions:,} B"
    assert total < 26_000, f"tools/list grew to {total:,} B"
