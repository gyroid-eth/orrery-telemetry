"""Recovery authenticates an exact owned row without enrollment or profile writes."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from fastmcp import Client
from sqlalchemy import select

from agentstack_mail import app, config, db
from agentstack_mail.models import Agent
from test_identity_contract import _configure_isolated_runtime, _payload


@pytest.mark.parametrize("mode", ["passthrough", "coerce", "always_auto"])
@pytest.mark.parametrize("case", ["owned", "missing", "unowned", "empty_owner", "wrong_token",
                                  "missing_token", "wrong_name", "wrong_project", "wrong_program"])
def test_existing_owner_authentication_never_creates_claims_or_updates(monkeypatch, tmp_path, mode, case):
    _configure_isolated_runtime(monkeypatch, tmp_path, mode=mode)
    project = str(tmp_path / "project")
    other_project = str(tmp_path / "other")
    token = "existing-owner-fixture-token"

    async def run():
        async with Client(app.build_mcp_server()) as client:
            ensured = _payload(await client.call_tool("ensure_project", {"human_key": project}))
            await client.call_tool("ensure_project", {"human_key": other_project})
            async with db.get_session() as session:
                agent = Agent(project_id=ensured["id"], name="LegacyResearcher", program="claude-code",
                              model="original-model", task_description="original task",
                              contact_policy="block_all", attachments_policy="file",
                              retired_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
                              registration_token=None if case == "unowned" else "" if case == "empty_owner" else token)
                session.add(agent)
                await session.commit()
                await session.refresh(agent)
                agent_id = agent.id

            async def rows():
                async with db.get_session() as session:
                    result = await session.execute(select(Agent).order_by(Agent.id))
                    return [row.model_dump(mode="json") for row in result.scalars()]

            before = await rows()
            archive = tmp_path / "archive" / "agents"
            def forbid_registration(*_args, **_kwargs):
                raise AssertionError("Authentication entered identity creation/profile mutation")
            monkeypatch.setattr(app, "_get_or_create_agent", forbid_registration)
            def files():
                return {str(path.relative_to(archive)): (path.read_bytes(), path.stat().st_mtime_ns)
                        for path in archive.rglob("*") if path.is_file()}
            original_archive = files()
            arguments = dict(project_key=project, name="LegacyResearcher", program="claude-code",
                             model="attempted-new-model", task_description="attempted new task",
                             attachments_policy="inline", registration_token=token,
                             existing_agent_id=agent_id)
            replacements = {
                "missing": {"existing_agent_id": agent_id + 100},
                "wrong_token": {"registration_token": "different-owner"},
                "missing_token": {"registration_token": None},
                "wrong_name": {"name": "legacyresearcher"},
                "wrong_project": {"project_key": other_project},
                "wrong_program": {"program": "codex"},
            }
            arguments.update(replacements.get(case, {}))
            result = await client.call_tool("register_agent", arguments, raise_on_error=False)
            assert result.is_error is (case != "owned")
            if case == "owned":
                response = _payload(result)
                assert response["id"] == agent_id and response["name"] == "LegacyResearcher"
                assert response["project_id"] == ensured["id"] and response["program"] == "claude-code"
                assert response["model_raw"] == "original-model" and response["retired_at"]
                assert token not in json.dumps(response)
            assert await rows() == before
            assert files() == original_archive

    try:
        asyncio.run(run())
    finally:
        asyncio.run(db.dispose_database_for_shutdown())
        db.reset_database_state()
        config.clear_settings_cache()
