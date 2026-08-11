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
LIVE_BUNDLE_SHA256 = "2265572de9ae1161c0be5e2681137d10205400cc01c3efe93bbcb16c30e37a1e"
LIVE_PATCH_SHA256 = "8f592e415af1cb00c8daea9b190fadf8f9dcfbaa6d4b2b957c8a690da05f9eac"
CUTOVER_PATCH_SHA256 = {
    "0001-orrery-mail-db-selector.patch": (
        "bc4b7d9d379c4770408bb45a09d8778307f1038ed5e679d1b71a3ad5c57506d1"
    ),
    "0002-dashboard-mail-cutover-selectors.patch": (
        "fb57b50157931255c9a9efb4dd1b7d1a93c3008374a10fd566d73d95883bb658"
    ),
    "0002b-dashboard-live-launchagent-selectors.patch": (
        "5df21b01757d5829b038ed785a72f248613f54be6d2ec12e4444feabcde9a470"
    ),
    "0003-dashboard-agentstack-mail-no-bearer.patch": (
        "42b95c21d5b71163bff7be842b5183b2ff4897d6598f7f60b27a939dc9485748"
    ),
    "0004a-dashboard-loopback-retire-exit.patch": (
        "f0c62d81f383951eb5daa4d6af3c9581fe8f5f9d4dbc37cb4420b9c1d3dd55c9"
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
    assert len(compatibility) == 24
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
    assert len(COMPATIBILITY_TOOLS) == 24
    assert (
        "authority is determined by endpoint, data roots, and ownership"
        in normalized_docs
    )


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


def test_cutover_runbook_names_every_machine_gate_exactly_once() -> None:
    manifest = json.loads(
        (
            PACKAGE
            / "fixtures"
            / "differential-expected-divergences-v2.json"
        ).read_text()
    )
    required_ids = manifest["cutover_gate"]["required_condition_ids"]
    runbook = (ROOT / "docs" / "agentstack-mail-cutover.md").read_text()

    assert len(required_ids) == len(set(required_ids)) == 14
    assert {
        condition_id: runbook.count(f"`{condition_id}`")
        for condition_id in required_ids
    } == {condition_id: 1 for condition_id in required_ids}
    assert "post-switch operational smoke check" in runbook
    assert "別の完了条件ではない" in runbook
    assert "suppressed 16" in runbook
    assert "22-tool" not in runbook


def test_cutover_runbook_pins_the_versioned_display_patch_chain() -> None:
    runbook = (ROOT / "docs" / "agentstack-mail-cutover.md").read_text()
    patch_root = ROOT / "docs" / "agentstack-mail-cutover-patches"

    for name, expected_digest in CUTOVER_PATCH_SHA256.items():
        assert hashlib.sha256((patch_root / name).read_bytes()).hexdigest() == (
            expected_digest
        )
        assert f"{expected_digest}  {name}" in runbook

    apply_body = runbook.split("apply_display_patches() {", 1)[1].split(
        "rollback_display_patches() {", 1
    )[0]
    rollback_body = runbook.split("rollback_display_patches() {", 1)[1].split(
        "```", 1
    )[0]
    names = list(CUTOVER_PATCH_SHA256)
    assert [apply_body.index(name) for name in names] == sorted(
        apply_body.index(name) for name in names
    )
    assert [rollback_body.index(name) for name in reversed(names)] == sorted(
        rollback_body.index(name) for name in reversed(names)
    )
    assert 'PATCH_DIR="$REPO/docs/agentstack-mail-cutover-patches"' in runbook
