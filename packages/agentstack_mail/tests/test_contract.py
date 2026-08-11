import json
from pathlib import Path

from agentstack_mail.contract import (
    COMPATIBILITY_TOOLS,
    CONTRACT_VERSION,
    ISOLATION_DEFAULTS,
    LEGACY_COLLISION_VALUES,
    LOCAL_ONLY_TOOLS,
    MODEL_COMPATIBILITY_TOOLS,
    NON_COMPATIBILITY_UPSTREAM_TOOLS,
    POLICY_EXCLUDED_UPSTREAM_TOOLS,
    RUNTIME_REQUIRED_TOOLS,
    SERVICE_IDENTITY,
)


def test_service_identity_is_independent() -> None:
    assert CONTRACT_VERSION == 1
    assert SERVICE_IDENTITY["distribution"] == "agentstack-mail"
    assert SERVICE_IDENTITY["python_package"] != LEGACY_COLLISION_VALUES["python_package"]
    assert (
        SERVICE_IDENTITY["mcp_provider_identity"]
        != LEGACY_COLLISION_VALUES["mcp_provider_identity"]
    )
    assert SERVICE_IDENTITY["claude_client_key"] == "mcp-agent-mail"
    assert SERVICE_IDENTITY["codex_client_key"] == "agent-mail"
    assert SERVICE_IDENTITY["client_key_policy"] == "preserve_existing"
    assert SERVICE_IDENTITY["mcp_provider_identity"] not in {
        SERVICE_IDENTITY["claude_client_key"],
        SERVICE_IDENTITY["codex_client_key"],
    }
    assert SERVICE_IDENTITY["environment_prefix"] == "AGENTSTACK_MAIL_"


def test_storage_and_network_defaults_do_not_collide() -> None:
    assert ISOLATION_DEFAULTS.port != LEGACY_COLLISION_VALUES["port"]
    assert ISOLATION_DEFAULTS.database != LEGACY_COLLISION_VALUES["database"]
    assert ISOLATION_DEFAULTS.archive != LEGACY_COLLISION_VALUES["archive"]
    assert ISOLATION_DEFAULTS.signals != LEGACY_COLLISION_VALUES["signals"]
    assert ISOLATION_DEFAULTS.mcp_url == "http://127.0.0.1:18765/mcp"
    assert ISOLATION_DEFAULTS.api_url == "http://127.0.0.1:18765/api/"


def test_compatibility_surface_matches_caller_audit() -> None:
    fixture_path = Path(__file__).parents[1] / "fixtures" / "compatibility-tools-v1.json"
    fixture = json.loads(fixture_path.read_text())

    assert fixture["contract_version"] == CONTRACT_VERSION
    assert set(fixture["runtime_required"]) == RUNTIME_REQUIRED_TOOLS
    assert set(fixture["model_compatibility"]) == MODEL_COMPATIBILITY_TOOLS
    assert set(fixture["compatibility_union"]) == COMPATIBILITY_TOOLS
    assert set(fixture["local_only_not_upstream"]) == LOCAL_ONLY_TOOLS
    assert (
        set(fixture["non_compatibility_upstream"])
        == NON_COMPATIBILITY_UPSTREAM_TOOLS
    )
    assert (
        set(fixture["policy_excluded_upstream"])
        == POLICY_EXCLUDED_UPSTREAM_TOOLS
    )
    assert len(RUNTIME_REQUIRED_TOOLS) == 12
    assert len(MODEL_COMPATIBILITY_TOOLS) == 23
    assert len(COMPATIBILITY_TOOLS) == 24
    assert "retire_agent" in RUNTIME_REQUIRED_TOOLS - MODEL_COMPATIBILITY_TOOLS
    assert "macro_contact_handshake" in MODEL_COMPATIBILITY_TOOLS
    assert {"search_messages", "summarize_thread"} <= MODEL_COMPATIBILITY_TOOLS
    assert "create_agent_identity" not in COMPATIBILITY_TOOLS
    assert "runtime_status" not in COMPATIBILITY_TOOLS
    assert POLICY_EXCLUDED_UPSTREAM_TOOLS <= NON_COMPATIBILITY_UPSTREAM_TOOLS


def test_live_tool_fixture_is_a_complete_contract_partition() -> None:
    fixture_root = Path(__file__).parents[1] / "fixtures"
    live = json.loads((fixture_root / "live-tools-list.json").read_text())
    live_names = {tool["name"] for tool in live["tools"]}

    assert live_names == COMPATIBILITY_TOOLS | NON_COMPATIBILITY_UPSTREAM_TOOLS
    assert not COMPATIBILITY_TOOLS & NON_COMPATIBILITY_UPSTREAM_TOOLS
