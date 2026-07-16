from __future__ import annotations

import os
import secrets

import pytest

from agentstack_codex_app.agent_mail_client import AgentMailClient, HttpJsonRpcTransport


pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.environ.get("AGENTSTACK_RUN_AGENT_MAIL_INTEGRATION") != "1",
    reason="set AGENTSTACK_RUN_AGENT_MAIL_INTEGRATION=1 to register a disposable identity",
)
def test_real_agent_mail_registration():
    endpoint = os.environ["AGENTSTACK_MCP_URL"]
    project_key = os.environ["AGENTSTACK_PROJECT_KEY"]
    bearer = os.environ.get("MCP_AGENT_MAIL_TOKEN")
    client = AgentMailClient(HttpJsonRpcTransport(endpoint, bearer_token=bearer))
    result = client.register_agent(
        project_key=project_key,
        model="integration-test",
        registration_token=secrets.token_urlsafe(32),
        task_description="Codex App integration test",
    )
    assert result.agent_name
