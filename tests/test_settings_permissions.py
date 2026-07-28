#!/usr/bin/env python3
"""Regression tests for permission merging into Claude Code settings.

Answers the 2026-07-22 tester question "is there an intended permissions
block?": there is now, it ships in hooks/settings.template.json, and the
installer merges it additively. Before this, merge_settings.py read only the
template's "hooks" key, so any permissions block would have been ignored.

Runnable two ways (no third-party dependency required):
    python3 tests/test_settings_permissions.py
    pytest tests/test_settings_permissions.py
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_MERGE = _ROOT / "scripts" / "lib" / "merge_settings.py"
_TEMPLATE = _ROOT / "hooks" / "settings.template.json"


def _merge(tmpdir: pathlib.Path, settings: dict | None, *extra: str,
           manifest: pathlib.Path | None = None) -> tuple[dict, dict, int]:
    settings_path = tmpdir / "settings.json"
    if settings is not None:
        settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    result_json = tmpdir / "result.json"
    cmd = [
        sys.executable, str(_MERGE),
        "--settings", str(settings_path),
        "--template", str(_TEMPLATE),
        "--hooks-dir", str(tmpdir / "hooks"),
        "--bin-dir", str(tmpdir / "bin"),
        "--skills-dir", str(tmpdir / "skills"),
        "--backup-dir", str(tmpdir / "backups"),
        "--result-json", str(result_json),
        *extra,
    ]
    if manifest is not None:
        cmd += ["--manifest", str(manifest)]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=False)
    written = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    detail = json.loads(result_json.read_text(encoding="utf-8")) if result_json.exists() else {}
    if proc.returncode != 0:
        detail = {"stderr": proc.stderr}
    return written, detail, proc.returncode


def test_template_ships_a_permissions_block():
    template = json.loads(_TEMPLATE.read_text(encoding="utf-8"))
    permissions = template["permissions"]
    assert permissions["allow"], "no allow rules"
    assert permissions["deny"], "no deny rules"
    # The startup calls the tester saw prompting every time.
    for tool in ("ensure_project", "register_agent", "fetch_inbox"):
        assert f"mcp__mcp-agent-mail__{tool}" in permissions["allow"], tool
    # Destructive coordination tools stay denied rather than pre-approved.
    for tool in ("hard_delete_project", "retire_agent", "purge_old_messages"):
        assert f"mcp__mcp-agent-mail__{tool}" in permissions["deny"], tool
    # No blanket approvals.
    assert "Bash(:*)" not in permissions["allow"]
    assert not any(rule.strip() in ("*", "Bash", "Bash(*)") for rule in permissions["allow"])


def test_merge_installs_permissions_into_fresh_settings():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        written, detail, rc = _merge(tmpdir, {})
        assert rc == 0, detail
        allow = written["permissions"]["allow"]
        assert "mcp__mcp-agent-mail__register_agent" in allow
        assert "mcp__mcp-agent-mail__retire_agent" in written["permissions"]["deny"]
        # The Bash rule is rendered with the real bin directory, not the token.
        assert any(str(tmpdir / "bin") in rule for rule in allow), allow
        assert "__AGENTSTACK_BIN_DIR__" not in json.dumps(written)
        assert detail["permissions"]["added"]["allow"], detail


def test_merge_preserves_user_rules_and_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        existing = {"permissions": {"allow": ["Bash(git status:*)"], "deny": ["Bash(rm:*)"]}}
        written, _, rc = _merge(tmpdir, existing)
        assert rc == 0
        assert written["permissions"]["allow"][0] == "Bash(git status:*)", "user rule moved"
        assert "Bash(rm:*)" in written["permissions"]["deny"], "user deny rule lost"

        before = json.dumps(written, sort_keys=True)
        written2, detail2, rc2 = _merge(tmpdir, written)
        assert rc2 == 0
        assert json.dumps(written2, sort_keys=True) == before, "second merge changed settings"
        assert detail2["permissions"]["added"]["allow"] == []


def test_remove_takes_back_only_what_the_installer_added():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        existing = {"permissions": {"allow": ["Bash(git status:*)"], "deny": ["Bash(rm:*)"]}}
        written, detail, rc = _merge(tmpdir, existing)
        assert rc == 0

        manifest = tmpdir / "manifest.json"
        manifest.write_text(json.dumps({"settings_merge": {
            "added_entries": detail.get("added_entries", []),
            "permissions": detail["permissions"],
        }}), encoding="utf-8")

        written_after, _, rc2 = _merge(tmpdir, written, "--remove", manifest=manifest)
        assert rc2 == 0
        permissions = written_after.get("permissions", {})
        assert permissions.get("allow") == ["Bash(git status:*)"], permissions
        assert permissions.get("deny") == ["Bash(rm:*)"], permissions


def test_bin_token_without_bin_dir_is_an_error_not_a_literal():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        settings_path = tmpdir / "settings.json"
        settings_path.write_text("{}", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(_MERGE),
             "--settings", str(settings_path),
             "--template", str(_TEMPLATE),
             "--hooks-dir", str(tmpdir / "hooks"),
             "--backup-dir", str(tmpdir / "backups")],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        assert proc.returncode != 0
        assert "--bin-dir" in proc.stderr
        assert settings_path.read_text(encoding="utf-8") == "{}", "settings rewritten on error"


def test_installer_passes_the_bin_dir():
    install = (_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "--bin-dir \"$BIN_DIR\"" in install


def _main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("\n" + ("ALL PASSED" if not failures else f"{failures} FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
