from __future__ import annotations

import os

import pytest

from agentstack_codex_app.protocol import AppServerClient


pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.environ.get("AGENTSTACK_RUN_CODEX_INTEGRATION") != "1",
    reason="set AGENTSTACK_RUN_CODEX_INTEGRATION=1 to start real codex app-server",
)
def test_real_app_server_initialize_and_thread_list():
    with AppServerClient(timeout=30) as client:
        result = client.thread_list(limit=1, use_state_db_only=True)
    assert isinstance(result.get("data"), list)
