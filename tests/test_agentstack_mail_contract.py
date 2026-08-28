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
        "31c36321aa0bd4ab40cfc6544ff024b38639a2dc3fc2ab423adb115701e38758"
    ),
    "0002-dashboard-mail-cutover-selectors.patch": (
        "97833bbe1f866a2dc8a05c8dae92b8be8a6db30b1375c9db607a8659b7d25240"
    ),
    "0002b-dashboard-live-launchagent-selectors.patch": (
        "5b3cd31c6fbc3fbf3a5c79f08998f0627b12d52d8db3f11bc32c7d3333d09d24"
    ),
    "0003-dashboard-agentstack-mail-no-bearer.patch": (
        "734778af233e1ba7833fbaebc574f7e5b6f76183a14c32b70d9e7aa73b1166f2"
    ),
    "0004a-dashboard-loopback-retire-exit.patch": (
        "b134a0953daaa554b2108a25ebc35fc2b94367600c1d6f61bbe0cd8eca447dea"
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


def test_distribution_contract_lists_every_runtime_module_and_cutover_test() -> None:
    verifier = runpy.run_path(str(PACKAGE / "tests" / "verify_artifact.py"))
    runtime_modules = {
        path.name for path in (PACKAGE_SRC / "agentstack_mail").glob("*.py")
    }

    assert verifier["REQUIRED_RUNTIME_MODULES"] == runtime_modules
    assert "/tests/test_cutover_client.py" in verifier["SDIST_REQUIRED_SUFFIXES"]


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

    assert len(required_ids) == len(set(required_ids)) == 11
    assert set(manifest["cutover_approval"]["descope"][
        "removed_required_condition_ids"
    ]) == {
        "data-migration-reconciliation",
        "rollback-revert-procedure",
        "notification-layout-consumer-compatibility",
    }
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
    assert "同じ既存keyと同じendpoint" not in runbook
    assert "既存keyとisolated candidate endpoint" in runbook
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
        "### documentation-only disposition（normative）"
    )
    assert runbook.count("assert_cutover_client_provenance || return 1") == 4
    assert 'archive.read("agentstack_mail/cutover_client.py")' in runbook
    assert (
        'CODEX_TOKEN_CENSUS="$MAINT/running-codex-token-census-'
        '$CANDIDATE_COMMIT.json"' in runbook
    )
    assert "write_running_codex_token_census" in runbook
    assert "not_present_unverified" in runbook
    assert "drift_process_count" in runbook
    assert "raw_values_emitted" in runbook
    assert "全sessionを個別にsealしたとは主張しない" in runbook


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
    label_match = re.search(
        r"^AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABEL=([^\s]+)$", runbook, re.MULTILINE
    )
    assert label_match is not None
    legacy_label = label_match.group(1)
    assert legacy_label.startswith("com.")
    assert legacy_label.endswith(".mcp-agent-mail")
    assert "AGENTSTACK_MAIL_LEGACY_LAUNCHD_RECEIPT=" in runbook
    assert "AGENTSTACK_MAIL_LEGACY_LAUNCHD_RECEIPT_SHA256=" in runbook
    assert 'binding = preflight.pop("legacy_launchd_receipt")' in runbook
    assert f'"definition_label": "{legacy_label}"' in runbook
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

    assert re.search(
        r"^REPO='/Users/[^/]+/OSS/worktrees/PluckyMailDifferential'$",
        runbook,
        re.MULTILINE,
    )
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
    assert 'assert set(observations) == {"new_ids_are_contiguous"}' in runbook
    assert 'assert type(observations["new_ids_are_contiguous"]) is bool' in runbook
    assert "正当な採番gapを不合格にせずreport-only observation" in runbook
