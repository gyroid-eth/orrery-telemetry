"""A hook the operator removed must not come back on the next install.

Reported by a tester on 2026-08-22, after patching two guards out of their
settings and finding them restored: the merge adds every template entry that is
absent, which cannot distinguish "never installed" from "installed, then removed
on purpose". The upgrade path silently undid a deliberate decision, and nothing
in the output said so.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MERGE = REPO_ROOT / "scripts" / "lib" / "merge_settings.py"
TEMPLATE = REPO_ROOT / "hooks" / "settings.template.json"

GUARD = "check-agent-registered.sh"


def _merge(tmp_path: Path, settings: Path, *extra: str) -> dict:
    result = subprocess.run(
        [
            sys.executable,
            str(MERGE),
            "--settings",
            str(settings),
            "--template",
            str(TEMPLATE),
            "--hooks-dir",
            str(tmp_path / "hooks"),
            "--bin-dir",
            str(tmp_path / "bin"),
            "--backup-dir",
            str(tmp_path / "backups"),
            "--installed-entries",
            str(tmp_path / "installed-entries.json"),
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    if "--dry-run" in extra:
        # A dry run prints a diff, not a result document.
        return {}
    return json.loads(result.stdout)


def _commands(settings: Path) -> list[str]:
    data = json.loads(settings.read_text(encoding="utf-8"))
    found = []
    for entries in (data.get("hooks") or {}).values():
        for entry in entries:
            for hook in entry.get("hooks", []):
                command = hook.get("command")
                if isinstance(command, str):
                    found.append(command)
    return found


def _drop(settings: Path, needle: str) -> None:
    data = json.loads(settings.read_text(encoding="utf-8"))
    for event, entries in list((data.get("hooks") or {}).items()):
        kept_entries = []
        for entry in entries:
            hooks = [h for h in entry.get("hooks", []) if needle not in (h.get("command") or "")]
            if hooks:
                entry["hooks"] = hooks
                kept_entries.append(entry)
        data["hooks"][event] = kept_entries
    settings.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_a_removed_hook_stays_removed(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")

    _merge(tmp_path, settings)
    assert any(GUARD in command for command in _commands(settings))

    _drop(settings, GUARD)
    assert not any(GUARD in command for command in _commands(settings))

    result = _merge(tmp_path, settings)
    assert not any(GUARD in command for command in _commands(settings)), (
        "the installer restored a hook the operator had removed"
    )
    respected = json.dumps(result.get("details", result), ensure_ascii=False)
    assert GUARD in respected and "respected_removals" in respected, (
        "the decision was silent; it has to be reported"
    )


def test_a_first_install_still_installs_everything(tmp_path: Path) -> None:
    """The null case: absent is only a removal if we put it there before.

    Without this, "never re-add what is missing" would pass the test above and
    install nothing at all.
    """
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    _merge(tmp_path, settings)
    assert any(GUARD in command for command in _commands(settings))


def test_an_operator_can_ask_for_it_back(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    _merge(tmp_path, settings)
    _drop(settings, GUARD)
    _merge(tmp_path, settings, "--restore-removed")
    assert any(GUARD in command for command in _commands(settings))


def test_a_dry_run_does_not_record_anything(tmp_path: Path) -> None:
    """A preview that writes state would make the next real run behave differently."""
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    _merge(tmp_path, settings, "--dry-run")
    assert not (tmp_path / "installed-entries.json").exists()
