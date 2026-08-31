import hashlib
import json
import re
import runpy
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
LIVE_BUNDLE_SHA256 = "2265572de9ae1161c0be5e2681137d10205400cc01c3efe93bbcb16c30e37a1e"
LIVE_PATCH_SHA256 = "8f592e415af1cb00c8daea9b190fadf8f9dcfbaa6d4b2b957c8a690da05f9eac"
CUTOVER_PATCH_SHA256 = {
    "0001-orrery-mail-db-selector.patch": (
        "fad125aff843eee373ce9a56182528f91dc005082edbd3236e49f51bc992e4b8"
    ),
    "0002-dashboard-mail-cutover-selectors.patch": (
        "6e2cf1ca26b9eb4b91182650f965d49db6822e7ded17a00572586387fa19b21d"
    ),
    "0002b-dashboard-live-launchagent-selectors.patch": (
        "357d44d9e27676271ac1b51d8546cb3e2c0091ccda086c2f4a21fdc061a24ddd"
    ),
    "0003-dashboard-agentstack-mail-no-bearer.patch": (
        "286e02563ef4d424d7036e350366e9cf320a3c90c4ba6eb0a7444774d90fecf1"
    ),
    "0004a-dashboard-loopback-retire-exit.patch": (
        "4fa69c9a13c1dd44b9625ab00b028c413efd24400a86a67332a9b573d0c2cde4"
    ),
}


def test_provenance_and_live_contract_fixture_are_present() -> None:
    license_text = (PACKAGE / "UPSTREAM_LICENSE").read_text()
    notice = (PACKAGE / "NOTICE.md").read_text()
    fixture_path = PACKAGE / "fixtures" / "live-tools-list.json"

    assert "Copyright (c) 2026 Jeffrey Emanuel" in license_text
    assert "OpenAI/Anthropic Rider" in license_text
    assert "b8251c1336e5fdca80a91b8b608d843df91b64e8" in notice
    assert hashlib.sha256(fixture_path.read_bytes()).hexdigest() == LIVE_TOOLS_SHA256

    live_payload = json.loads(fixture_path.read_text())
    contract_payload = json.loads(
        (PACKAGE / "fixtures" / "compatibility-tools-v1.json").read_text()
    )
    names = {tool["name"] for tool in live_payload["tools"]}
    compatibility = set(contract_payload["compatibility_union"])
    assert len(names) == 40
    assert len(compatibility) == 25
    assert compatibility <= names
    assert names == compatibility | NON_COMPATIBILITY_UPSTREAM_TOOLS
    assert "create_agent_identity" not in compatibility
    assert "runtime_status" not in compatibility


def test_contract_documents_isolated_namespaces() -> None:
    docs = (ROOT / "docs" / "agentstack-mail.md").read_text()
    normalized_docs = " ".join(docs.lower().split())

    assert SERVICE_IDENTITY["mcp_provider_identity"] == "agentstack-mail"
    assert SERVICE_IDENTITY["claude_client_key"] == "mcp-agent-mail"
    assert SERVICE_IDENTITY["codex_client_key"] == "agent-mail"
    assert SERVICE_IDENTITY["client_key_policy"] == "preserve_existing"
    assert SERVICE_IDENTITY["launchd_label"] == "org.orrery.mail"
    assert len(COMPATIBILITY_TOOLS) == 25
    assert (
        "authority is determined by endpoint, data roots, and ownership"
        in normalized_docs
    )



def test_distribution_contract_lists_every_runtime_module_and_cutover_test() -> None:
    verifier = runpy.run_path(str(PACKAGE / "tests" / "verify_artifact.py"))
    runtime_modules = {
        path.name for path in (PACKAGE_SRC / "agentstack_mail").glob("*.py")
    }

    assert verifier["REQUIRED_RUNTIME_MODULES"] == runtime_modules
    assert "/tests/test_cutover_client.py" in verifier["SDIST_REQUIRED_SUFFIXES"]






