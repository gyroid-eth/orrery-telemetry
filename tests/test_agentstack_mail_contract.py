import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages" / "agentstack_mail"
PACKAGE_SRC = PACKAGE / "src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

from agentstack_mail.contract import (
    COMPATIBILITY_TOOLS,
    NON_COMPATIBILITY_UPSTREAM_TOOLS,
    SERVICE_IDENTITY,
)

LIVE_TOOLS_SHA256 = "6ea7dabf41f71091161fa1fcb8a4073a383a65c7bba4785306217fd35f9e8332"
LIVE_BUNDLE_SHA256 = "55f03ea48a3279f090c4b93436af1d55f912c75c3f985e4ba06a8b95d39f7670"
LIVE_PATCH_SHA256 = "8f592e415af1cb00c8daea9b190fadf8f9dcfbaa6d4b2b957c8a690da05f9eac"


def test_provenance_and_live_contract_fixture_are_present() -> None:
    license_text = (PACKAGE / "UPSTREAM_LICENSE").read_text()
    notice = (PACKAGE / "NOTICE.md").read_text()
    fixture_path = PACKAGE / "fixtures" / "live-tools-list.json"

    assert "Copyright (c) 2026 Jeffrey Emanuel" in license_text
    assert "OpenAI/Anthropic Rider" in license_text
    assert "ad0e4788967d809979fa25004cf52545fdcd888a" in notice
    assert hashlib.sha256(fixture_path.read_bytes()).hexdigest() == LIVE_TOOLS_SHA256

    live_payload = json.loads(fixture_path.read_text())
    contract_payload = json.loads(
        (PACKAGE / "fixtures" / "compatibility-tools-v1.json").read_text()
    )
    names = {tool["name"] for tool in live_payload["tools"]}
    compatibility = set(contract_payload["compatibility_union"])
    assert len(names) == 40
    assert len(compatibility) == 22
    assert compatibility <= names
    assert names == compatibility | NON_COMPATIBILITY_UPSTREAM_TOOLS
    assert "create_agent_identity" not in compatibility
    assert "runtime_status" not in compatibility


def test_contract_documents_isolated_namespaces() -> None:
    docs = (ROOT / "docs" / "agentstack-mail.md").read_text()

    assert SERVICE_IDENTITY["mcp_server_key"] == "agentstack-mail"
    assert len(COMPATIBILITY_TOOLS) == 22
    assert "not registered as an alias by default" in docs


def test_tracked_live_source_is_content_addressed() -> None:
    provenance = PACKAGE / "provenance"

    assert (
        hashlib.sha256((provenance / "live-head.bundle").read_bytes()).hexdigest()
        == LIVE_BUNDLE_SHA256
    )
    assert (
        hashlib.sha256(
            (provenance / "working-tree-tracked.patch").read_bytes()
        ).hexdigest()
        == LIVE_PATCH_SHA256
    )
