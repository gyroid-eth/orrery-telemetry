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
    assert SERVICE_IDENTITY["launchd_label"] == "org.orrery.mail"
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


def test_cutover_runbook_pins_selector_and_unchanged_client_header_contract() -> None:
    runbook = (ROOT / "docs" / "agentstack-mail-cutover.md").read_text()
    probe = runbook.split("bounded_mail_probe() {", 1)[1].split("\n}\n", 1)[0]
    selector_census = (
        "permission `allow`のraw occurrenceは68件（global "
        "`~/.claude/settings.json` 28件、local `settings.local.json` "
        "15ファイル40件）、hook matcherは2件で合計70件である。"
        "distinct unionは34（global 28 / local 28 / local-only 6）。"
    )

    assert runbook.count(selector_census) == 3
    assert "39件" not in runbook
    assert "旧「70件」は誤集計" not in runbook
    assert "URLと認証だけを切り替える" not in runbook
    assert (
        "client設定はkey / URL / port / path / tokenのいずれも変更しない"
        in runbook
    )
    assert '"Authorization": authorization' in probe
    assert probe.index(
        "authorization = read_pinned_client_authorization("
    ) < probe.index("urllib.request.Request")
    assert "print(authorization)" not in probe
    assert "{last}" not in probe
    assert 'raise SystemExit("bounded MCP read probe failed")' in probe
    assert "headerなしや現在値への追随をせずfail-closed" in runbook
    assert "この同じ関数をH9、RB4、R6で使う" in runbook
    assert (
        "生のtokenはstdout、stderr、receipt、assertion messageへ出さず" in runbook
    )
    assert runbook.count("bounded_mail_probe \\") == 3
    assert runbook.count("assert_client_config_seal >") == 3
    assert 'CANDIDATE_VENV="$CANDIDATE_ARTIFACT_ROOT/venv"' in runbook
    assert 'CUTOVER_PYTHON="$CANDIDATE_VENV/bin/python"' in runbook
    assert "CUTOVER_PYTHONPATH" not in runbook
    assert runbook.index("initialize_client_config_seal || exit 1") < runbook.index(
        "### 現行v1の停止点（normative）"
    )
    assert runbook.count("assert_cutover_client_provenance || return 1") == 3
    assert 'archive.read("agentstack_mail/cutover_client.py")' in runbook


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


def test_cutover_runbook_keeps_the_production_endpoint_stable() -> None:
    runbook = (ROOT / "docs" / "agentstack-mail-cutover.md").read_text()

    assert "AGENTSTACK_MAIL_HTTP_PORT=8765" in runbook
    assert "AGENTSTACK_MAIL_HTTP_PATH=/api/" in runbook
    assert (
        "AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABEL=com.operator.mcp-agent-mail"
        in runbook
    )
    assert "AGENTSTACK_MAIL_LEGACY_LAUNCHD_RECEIPT=" in runbook
    assert "AGENTSTACK_MAIL_LEGACY_LAUNCHD_RECEIPT_SHA256=" in runbook
    assert 'binding = preflight.pop("legacy_launchd_receipt")' in runbook
    assert '"definition_label": "com.operator.mcp-agent-mail"' in runbook
    assert "ここが最初の必須切替成功gate" in runbook
    assert "H9は省略不可" in runbook
    assert "healthだけの成功を切替成功扱いにしない" in runbook
    assert "'http://127.0.0.1:8765/api/' 8765" in runbook
    assert "AGENTSTACK_MAIL_HTTP_PORT=18765" not in runbook
    assert "AGENTSTACK_MAIL_HTTP_PATH=/mcp" not in runbook
    assert '"new_mcp_url": "http://127.0.0.1:18765/mcp"' not in runbook
    assert "新 job/18765" not in runbook
    assert "新job/18765" not in runbook


def test_cutover_runbook_pins_the_accepted_restore_observer_contract() -> None:
    runbook = (ROOT / "docs" / "agentstack-mail-cutover.md").read_text()

    assert "REPO='/Users/operator/OSS/worktrees/PluckyMailDifferential'" in runbook
    assert '"$EVIDENCE_BIN" restore-rehearsal' in runbook
    assert "--backup-main-size 67293184" in runbook
    assert (
        "--backup-main-sha256 "
        "c80bdf9ddb59ab712c0ef23a60be08fbe8ec78f4fa523f02918fb1bae35eea02"
        in runbook
    )
    assert (
        "--backup-wal-sha256 "
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        in runbook
    )
    assert (
        "--backup-shm-sha256 "
        "fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb"
        in runbook
    )
    assert (
        "--expected-logical-sha256 "
        "afb50ad0a331b233c865db8d0e9512248c9ef5d75aa129c859198d9002317818"
        in runbook
    )
    assert "--expected-message-max-id 8829" in runbook
    assert (
        "--expected-message-sha256 "
        "1cc1f6636c3755d1404c2df953b64cc00e0e8a168ae75b1ccd2dfeada1430713"
        in runbook
    )
    assert "両fileがregular・mode 0400・nlink 1" in runbook
    assert 'assert info.st_nlink == 1' in runbook
    assert "second_prepared_alias_unlink_returns" in runbook
    assert "command完了前に照合子を並行起動しない" in runbook
