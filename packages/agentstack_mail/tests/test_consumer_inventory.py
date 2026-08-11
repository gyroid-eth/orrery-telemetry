from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from pathspec import GitIgnoreSpec

from agentstack_mail.consumer_inventory import (
    INVENTORY_NAME,
    SEAL_NAME,
    InventoryCollectionError,
    collect,
    main,
)


def _desired(home: Path) -> dict[str, str]:
    old_home = home / ".mcp_agent_mail"
    new_home = home / ".agentstack" / "mail"
    return {
        "legacy_mcp_url": "http://127.0.0.1:8765/mcp",
        "new_mcp_url": "http://127.0.0.1:18765/mcp",
        "legacy_mail_db": str(home / "mcp_agent_mail" / "storage.sqlite3"),
        "new_mail_db": str(new_home / "storage.sqlite3"),
        "legacy_mail_env": str(home / "mcp_agent_mail" / ".env"),
        "new_mail_env": str(home / ".agentstack" / "agentstack-mail.env"),
        "legacy_mail_home": str(old_home),
        "new_mail_home": str(new_home),
        "legacy_signals_dir": str(old_home / "signals"),
        "new_signals_dir": str(new_home / "signals"),
    }


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, dict[Path, bytes]]:
    root = tmp_path / "vault"
    positive = root / ".claude" / "settings.json"
    ignored = (
        root
        / "05_Agents"
        / "Life"
        / "attachments"
        / ".claude"
        / "settings.local.json"
    )
    positive.parent.mkdir(parents=True)
    ignored.parent.mkdir(parents=True)
    positive.write_text(
        '{"permissions":{"allow":["mcp__mcp-agent-mail__fetch_inbox"]}}\n',
        encoding="utf-8",
    )
    ignored.write_text(
        '{"permissions":{"allow":["mcp__mcp-agent-mail__mark_message_read"]}}\n',
        encoding="utf-8",
    )
    (root / ".gitignore").write_text("**/attachments/\n", encoding="utf-8")
    unrelated = root / ".hidden" / "unrelated.txt"
    unrelated.parent.mkdir()
    unrelated.write_text("not a consumer\n", encoding="utf-8")
    for path in (positive, ignored):
        path.chmod(0o600)
    spec = tmp_path / "spec.json"
    payload = {
        "schema_version": 1,
        "desired": _desired(tmp_path),
        "roots": [str(root)],
        "rules": [
            {
                "root": str(root),
                "glob": ".claude/settings.json",
                "kind": "claude_settings",
            },
            {
                "root": str(root),
                "glob": "**/.claude/settings.local.json",
                "kind": "claude_settings",
            },
        ],
        "excluded_paths": [],
        "controls": {
            "known_positive_selector": {
                "path": str(positive),
                "selector": "mcp__mcp-agent-mail__fetch_inbox",
            },
            "known_ignored_path": str(ignored),
        },
    }
    spec.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return spec, tmp_path / "bundle", positive, ignored, {
        positive: positive.read_bytes(),
        ignored: ignored.read_bytes(),
    }


def test_collect_includes_hidden_gitignored_path_and_seals_both_controls(
    tmp_path: Path,
) -> None:
    spec, bundle, positive, ignored, before = _fixture(tmp_path)

    result = collect(spec, bundle, hidden=True, no_ignore=True)

    ignore_spec = GitIgnoreSpec.from_lines(
        (positive.parents[1] / ".gitignore").read_text(encoding="utf-8").splitlines(),
    )
    assert ignore_spec.match_file(ignored.relative_to(positive.parents[1]).as_posix())
    assert result["status"] == "collected"
    assert result["consumer_count"] == 2
    inventory = json.loads((bundle / INVENTORY_NAME).read_text(encoding="utf-8"))
    assert inventory["consumers"] == [
        {"kind": "claude_settings", "path": str(positive)},
        {"kind": "claude_settings", "path": str(ignored)},
    ]
    seal = json.loads((bundle / SEAL_NAME).read_text(encoding="utf-8"))
    assert seal["collection_semantics"] == {
        "hidden": True,
        "no_ignore": True,
        "required_cli_flags": ["--hidden", "--no-ignore"],
    }
    assert seal["controls"]["search_liveness"]["status"] == "pass"
    assert seal["controls"]["ignored_path_completeness"] == {
        "status": "pass",
        "path": str(ignored),
    }
    assert seal["counts"]["matched_consumers"] == 2
    assert seal["counts"]["scanned_files"] == 4
    assert set(item["path"] for item in seal["files"]) == {
        str(positive),
        str(ignored),
    }
    assert all(path.read_bytes() == payload for path, payload in before.items())
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o700
    assert stat.S_IMODE((bundle / INVENTORY_NAME).stat().st_mode) == 0o400
    assert stat.S_IMODE((bundle / SEAL_NAME).stat().st_mode) == 0o400


@pytest.mark.parametrize(
    ("hidden", "no_ignore"),
    [(False, True), (True, False), (False, False)],
)
def test_collect_requires_both_hidden_and_no_ignore_semantics(
    tmp_path: Path, hidden: bool, no_ignore: bool
) -> None:
    spec, bundle, *_ = _fixture(tmp_path)
    with pytest.raises(InventoryCollectionError, match="both --hidden and --no-ignore"):
        collect(spec, bundle, hidden=hidden, no_ignore=no_ignore)
    assert not bundle.exists()


def test_cli_rejects_omitted_no_ignore_flag(tmp_path: Path) -> None:
    spec, bundle, *_ = _fixture(tmp_path)
    with pytest.raises(SystemExit) as raised:
        main(["--spec", str(spec), "--bundle", str(bundle), "--hidden"])
    assert raised.value.code == 2
    assert not bundle.exists()


@pytest.mark.parametrize("control", ["positive", "ignored"])
def test_control_failure_does_not_publish_partial_bundle(
    tmp_path: Path, control: str
) -> None:
    spec, bundle, _, ignored, _ = _fixture(tmp_path)
    payload = json.loads(spec.read_text(encoding="utf-8"))
    if control == "positive":
        payload["controls"]["known_positive_selector"]["selector"] = (
            "mcp__mcp-agent-mail__definitely_absent"
        )
    else:
        missing = ignored.parent / "missing-settings.local.json"
        payload["controls"]["known_ignored_path"] = str(missing)
    spec.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    expected = "known-positive" if control == "positive" else "ignored-path"
    with pytest.raises(InventoryCollectionError, match=expected):
        collect(spec, bundle, hidden=True, no_ignore=True)
    assert not bundle.exists()


def test_bounded_scan_fails_before_publication(tmp_path: Path) -> None:
    spec, bundle, *_ = _fixture(tmp_path)
    with pytest.raises(InventoryCollectionError, match="file limit"):
        collect(spec, bundle, hidden=True, no_ignore=True, max_files=1)
    assert not bundle.exists()
