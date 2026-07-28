#!/usr/bin/env python3
"""Safely merge claude-agent-stack hooks and skills into Claude Code settings."""

from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import json
import os
import pathlib
import shlex
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any


HOOKS_TOKEN = "__AGENTSTACK_HOOKS_DIR__"
BIN_TOKEN = "__AGENTSTACK_BIN_DIR__"
IRREVERSIBLE_SESSION_END_WORDS = ("retire", "kill", "hard_delete")
PERMISSION_KINDS = ("allow", "deny")


class MergeError(RuntimeError):
    """Expected merge failure that must not rewrite settings."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely merge or remove claude-agent-stack hook entries and skills directories.",
    )
    parser.add_argument("--settings", required=True, help="Claude settings.json path")
    parser.add_argument("--template", help="settings.template.json path")
    parser.add_argument("--hooks-dir", required=True, help="Installed hooks directory")
    parser.add_argument("--bin-dir", help="Installed bin directory (for permission rules)")
    parser.add_argument("--skills-dir", help="Installed skills directory")
    parser.add_argument("--backup-dir", required=True, help="Backup root directory")
    parser.add_argument("--manifest", help="Manifest whose recorded entries constrain --remove")
    parser.add_argument("--result-json", help="Write machine-readable operation result")
    parser.add_argument("--dry-run", action="store_true", help="Print diff without writing")
    parser.add_argument("--remove", action="store_true", help="Remove recorded agentstack entries")
    return parser.parse_args()


def sha256_bytes(raw: bytes | None) -> str | None:
    if raw is None:
        return None
    return hashlib.sha256(raw).hexdigest()


def dump_settings(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def read_settings(path: pathlib.Path) -> tuple[dict[str, Any], bytes | None]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, None

    if raw.strip() == b"":
        return {}, raw

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MergeError(f"settings is not valid UTF-8: {path}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MergeError(f"settings is not valid JSON: {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise MergeError(f"settings top-level value must be an object: {path}")

    return data, raw


def render_template(path: pathlib.Path, hooks_dir: pathlib.Path,
                    bin_dir: pathlib.Path | None) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise MergeError(f"template not found: {path}") from exc

    rendered = text.replace(HOOKS_TOKEN, str(hooks_dir))
    if BIN_TOKEN in rendered:
        if bin_dir is None:
            raise MergeError(f"template uses {BIN_TOKEN} but --bin-dir was not given")
        rendered = rendered.replace(BIN_TOKEN, str(bin_dir))
    try:
        data = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise MergeError(f"template is not valid JSON after token replacement: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MergeError(f"template top-level value must be an object: {path}")
    return data


def load_template_permissions(template: dict[str, Any], path: pathlib.Path) -> dict[str, list[str]]:
    permissions = template.get("permissions", {})
    if not isinstance(permissions, dict):
        raise MergeError(f"template permissions must be an object: {path}")
    checked: dict[str, list[str]] = {}
    for kind in PERMISSION_KINDS:
        rules = permissions.get(kind, [])
        if not isinstance(rules, list):
            raise MergeError(f"template permissions.{kind} must be an array: {path}")
        for rule in rules:
            if not isinstance(rule, str) or not rule:
                raise MergeError(f"template permissions.{kind} entries must be non-empty strings: {path}")
        checked[kind] = rules
    return checked


def load_template_hooks(data: dict[str, Any], path: pathlib.Path) -> dict[str, list[dict[str, Any]]]:
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise MergeError(f"template hooks must be an object: {path}")

    checked: dict[str, list[dict[str, Any]]] = {}
    for event, entries in hooks.items():
        if not isinstance(event, str):
            raise MergeError(f"template hook event must be a string: {path}")
        if not isinstance(entries, list):
            raise MergeError(f"template hooks.{event} must be an array: {path}")
        checked_entries: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise MergeError(f"template hooks.{event} entries must be objects: {path}")
            validate_hook_entry(entry, f"template hooks.{event}")
            checked_entries.append(entry)
        checked[event] = checked_entries

    validate_session_end_safety(checked)
    return checked


def validate_hook_entry(entry: dict[str, Any], where: str) -> None:
    hooks = entry.get("hooks")
    if not isinstance(hooks, list) or not hooks:
        raise MergeError(f"{where} entry must contain a non-empty hooks array")
    for hook in hooks:
        if not isinstance(hook, dict):
            raise MergeError(f"{where} hooks must be objects")
        if hook.get("type") == "command" and not isinstance(hook.get("command"), str):
            raise MergeError(f"{where} command hook must have a string command")


def validate_session_end_safety(template_hooks: dict[str, list[dict[str, Any]]]) -> None:
    for entry in template_hooks.get("SessionEnd", []):
        for command in command_values(entry):
            lowered = command.lower()
            for word in IRREVERSIBLE_SESSION_END_WORDS:
                if word in lowered:
                    raise MergeError(f"refusing irreversible SessionEnd command: {command}")


def command_values(entry: dict[str, Any]) -> list[str]:
    commands: list[str] = []
    hooks = entry.get("hooks", [])
    if not isinstance(hooks, list):
        return commands
    for hook in hooks:
        if isinstance(hook, dict) and hook.get("type") == "command":
            command = hook.get("command")
            if isinstance(command, str):
                commands.append(command)
    return commands


def matcher_value(entry: dict[str, Any]) -> str | None:
    matcher = entry.get("matcher")
    return matcher if isinstance(matcher, str) else None


def entry_key(event: str, matcher: str | None, command: str) -> dict[str, str | None]:
    return {"event": event, "matcher": matcher, "command": command}


def key_tuple(key: dict[str, Any]) -> tuple[str, str | None, str]:
    event = key.get("event")
    command = key.get("command")
    matcher = key.get("matcher")
    if not isinstance(event, str) or not isinstance(command, str):
        raise MergeError("manifest entry keys must include string event and command")
    if matcher is not None and not isinstance(matcher, str):
        raise MergeError("manifest entry matcher must be a string or null")
    return event, matcher, command


def ensure_target_hooks(settings: dict[str, Any]) -> dict[str, Any]:
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise MergeError("settings hooks must be an object; refusing to repair it")
    return hooks


def event_entries(hooks: dict[str, Any], event: str) -> list[dict[str, Any]]:
    entries = hooks.setdefault(event, [])
    if not isinstance(entries, list):
        raise MergeError(f"settings hooks.{event} must be an array; refusing to repair it")
    for entry in entries:
        if not isinstance(entry, dict):
            raise MergeError(f"settings hooks.{event} entries must be objects")
        validate_hook_entry(entry, f"settings hooks.{event}")
    return entries


def command_exists(entries: list[dict[str, Any]], matcher: str | None, command: str) -> bool:
    for entry in entries:
        if matcher_value(entry) != matcher:
            continue
        if command in command_values(entry):
            return True
    return False


def merge_template_entries(
    original: dict[str, Any],
    template_hooks: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, list[dict[str, str | None]]]]:
    settings = copy.deepcopy(original)
    hooks = ensure_target_hooks(settings)
    added: list[dict[str, str | None]] = []
    skipped_existing: list[dict[str, str | None]] = []

    for event, template_entries in template_hooks.items():
        if not template_entries:
            continue
        entries = event_entries(hooks, event)
        for template_entry in template_entries:
            matcher = matcher_value(template_entry)
            commands = command_values(template_entry)
            if not commands:
                raise MergeError(f"template hooks.{event} entry has no command hooks")

            missing_commands = [
                command for command in commands if not command_exists(entries, matcher, command)
            ]
            for command in commands:
                key = entry_key(event, matcher, command)
                if command in missing_commands:
                    added.append(key)
                else:
                    skipped_existing.append(key)

            if not missing_commands:
                continue

            new_entry = copy.deepcopy(template_entry)
            if len(missing_commands) != len(commands):
                new_hooks = []
                for hook in new_entry.get("hooks", []):
                    if (
                        isinstance(hook, dict)
                        and hook.get("type") == "command"
                        and hook.get("command") not in missing_commands
                    ):
                        continue
                    new_hooks.append(hook)
                new_entry["hooks"] = new_hooks
            entries.append(new_entry)

    return settings, {"added_entries": added, "skipped_existing": skipped_existing}


def ensure_target_permissions(settings: dict[str, Any], kind: str) -> list[str]:
    permissions = settings.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise MergeError("settings permissions must be an object; refusing to repair it")
    rules = permissions.setdefault(kind, [])
    if not isinstance(rules, list):
        raise MergeError(f"settings permissions.{kind} must be an array; refusing to repair it")
    for rule in rules:
        if not isinstance(rule, str):
            raise MergeError(f"settings permissions.{kind} entries must be strings")
    return rules


def merge_permissions(
    settings: dict[str, Any],
    template_permissions: dict[str, list[str]],
) -> dict[str, dict[str, list[str]]]:
    """Append missing rules; never reorder, rewrite or drop the user's own."""
    added: dict[str, list[str]] = {kind: [] for kind in PERMISSION_KINDS}
    skipped: dict[str, list[str]] = {kind: [] for kind in PERMISSION_KINDS}
    for kind in PERMISSION_KINDS:
        template_rules = template_permissions.get(kind, [])
        if not template_rules:
            continue
        rules = ensure_target_permissions(settings, kind)
        for rule in template_rules:
            if rule in rules:
                skipped[kind].append(rule)
                continue
            rules.append(rule)
            added[kind].append(rule)
    # Do not leave an empty permissions object behind if nothing was added.
    permissions = settings.get("permissions")
    if isinstance(permissions, dict):
        for kind in list(permissions.keys()):
            if permissions[kind] == [] and kind in PERMISSION_KINDS:
                del permissions[kind]
        if not permissions:
            del settings["permissions"]
    return {"added": added, "skipped_existing": skipped}


def remove_permissions(
    settings: dict[str, Any],
    allowed: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Remove only the rules this installer recorded as its own."""
    removed: dict[str, list[str]] = {kind: [] for kind in PERMISSION_KINDS}
    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        return removed
    for kind in PERMISSION_KINDS:
        targets = set(allowed.get(kind, []))
        if not targets:
            continue
        rules = permissions.get(kind)
        if not isinstance(rules, list):
            continue
        kept = []
        for rule in rules:
            if isinstance(rule, str) and rule in targets:
                removed[kind].append(rule)
            else:
                kept.append(rule)
        if kept:
            permissions[kind] = kept
        else:
            del permissions[kind]
    if not permissions:
        del settings["permissions"]
    return removed


def manifest_permissions(path: pathlib.Path | None) -> dict[str, list[str]]:
    merge = manifest_settings_merge(path) if path is not None else None
    if not merge:
        return {kind: [] for kind in PERMISSION_KINDS}
    raw = merge.get("permissions", {})
    if not isinstance(raw, dict):
        raise MergeError("manifest settings_merge.permissions must be an object")
    added = raw.get("added", {})
    if not isinstance(added, dict):
        raise MergeError("manifest settings_merge.permissions.added must be an object")
    result: dict[str, list[str]] = {}
    for kind in PERMISSION_KINDS:
        rules = added.get(kind, [])
        if not isinstance(rules, list):
            raise MergeError(f"manifest settings_merge.permissions.added.{kind} must be an array")
        result[kind] = [rule for rule in rules if isinstance(rule, str)]
    return result


def skills_dir_key(skills_dir: pathlib.Path) -> str:
    return str(skills_dir)


def ensure_target_skills_directories(settings: dict[str, Any]) -> list[str]:
    skills_dirs = settings.setdefault("skillsDirectories", [])
    if not isinstance(skills_dirs, list):
        raise MergeError("settings skillsDirectories must be an array; refusing to repair it")
    for item in skills_dirs:
        if not isinstance(item, str):
            raise MergeError("settings skillsDirectories entries must be strings")
    return skills_dirs


def merge_skills_directory(
    settings: dict[str, Any],
    skills_dir: pathlib.Path,
) -> dict[str, list[str]]:
    value = skills_dir_key(skills_dir)
    skills_dirs = ensure_target_skills_directories(settings)
    if value in skills_dirs:
        return {"added": [], "skipped_existing": [value]}
    skills_dirs.append(value)
    return {"added": [value], "skipped_existing": []}


def path_under(path: str, root: pathlib.Path) -> bool:
    if not path.startswith(("/", "~")):
        return False
    candidate = os.path.abspath(os.path.expanduser(path))
    root_abs = os.path.abspath(os.path.expanduser(str(root)))
    try:
        return os.path.commonpath([candidate, root_abs]) == root_abs
    except ValueError:
        return False


def command_owned_by_hooks_dir(command: str, hooks_dir: pathlib.Path) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    return any(path_under(token, hooks_dir) for token in tokens)


def manifest_settings_merge(path: pathlib.Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MergeError(f"manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise MergeError(f"manifest is not valid JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MergeError(f"manifest top-level value must be an object: {path}")

    merge = data.get("settings_merge")
    if not isinstance(merge, dict):
        return None
    return merge


def manifest_entry_keys(path: pathlib.Path | None) -> set[tuple[str, str | None, str]] | None:
    if path is None:
        return None
    merge = manifest_settings_merge(path)
    if merge is None:
        return set()
    raw_entries = merge.get("added_entries", merge.get("entries", []))
    if not isinstance(raw_entries, list):
        raise MergeError("manifest settings_merge.added_entries must be an array")
    return {key_tuple(entry) for entry in raw_entries if isinstance(entry, dict)}


def manifest_skills_dirs(path: pathlib.Path | None) -> set[str] | None:
    if path is None:
        return None
    merge = manifest_settings_merge(path)
    if merge is None:
        return set()
    raw_skills_dirs = merge.get("skills_dirs", {})
    if raw_skills_dirs in ({}, None):
        return set()
    if not isinstance(raw_skills_dirs, dict):
        raise MergeError("manifest settings_merge.skills_dirs must be an object")
    added = raw_skills_dirs.get("added", [])
    if not isinstance(added, list):
        raise MergeError("manifest settings_merge.skills_dirs.added must be an array")
    return {item for item in added if isinstance(item, str)}


def should_remove_command(
    event: str,
    matcher: str | None,
    command: str,
    hooks_dir: pathlib.Path,
    allowed_keys: set[tuple[str, str | None, str]] | None,
) -> bool:
    if not command_owned_by_hooks_dir(command, hooks_dir):
        return False
    if allowed_keys is None:
        return True
    return (event, matcher, command) in allowed_keys


def remove_agentstack_entries(
    original: dict[str, Any],
    hooks_dir: pathlib.Path,
    allowed_keys: set[tuple[str, str | None, str]] | None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, str | None]]]]:
    settings = copy.deepcopy(original)
    hooks = settings.get("hooks")
    if hooks is None:
        return settings, {"removed_entries": []}
    if not isinstance(hooks, dict):
        raise MergeError("settings hooks must be an object; refusing to repair it")

    removed: list[dict[str, str | None]] = []
    for event in list(hooks.keys()):
        entries = hooks[event]
        if not isinstance(event, str):
            raise MergeError("settings hook events must be strings")
        if not isinstance(entries, list):
            raise MergeError(f"settings hooks.{event} must be an array; refusing to repair it")

        new_entries: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise MergeError(f"settings hooks.{event} entries must be objects")
            validate_hook_entry(entry, f"settings hooks.{event}")
            matcher = matcher_value(entry)
            kept_hooks: list[Any] = []
            removed_from_entry = False
            for hook in entry.get("hooks", []):
                if (
                    isinstance(hook, dict)
                    and hook.get("type") == "command"
                    and isinstance(hook.get("command"), str)
                    and should_remove_command(
                        event,
                        matcher,
                        hook["command"],
                        hooks_dir,
                        allowed_keys,
                    )
                ):
                    removed.append(entry_key(event, matcher, hook["command"]))
                    removed_from_entry = True
                    continue
                kept_hooks.append(hook)

            if kept_hooks:
                if removed_from_entry:
                    new_entry = copy.deepcopy(entry)
                    new_entry["hooks"] = kept_hooks
                    new_entries.append(new_entry)
                else:
                    new_entries.append(entry)
            elif not removed_from_entry:
                new_entries.append(entry)

        if new_entries:
            hooks[event] = new_entries
        else:
            del hooks[event]

    if not hooks:
        del settings["hooks"]

    return settings, {"removed_entries": removed}


def remove_skills_directories(
    settings: dict[str, Any],
    allowed_dirs: set[str],
) -> dict[str, list[str]]:
    if not allowed_dirs:
        return {"removed": []}

    raw_skills_dirs = settings.get("skillsDirectories")
    if raw_skills_dirs is None:
        return {"removed": []}
    if not isinstance(raw_skills_dirs, list):
        raise MergeError("settings skillsDirectories must be an array; refusing to repair it")
    for item in raw_skills_dirs:
        if not isinstance(item, str):
            raise MergeError("settings skillsDirectories entries must be strings")

    removed: list[str] = []
    kept: list[str] = []
    for item in raw_skills_dirs:
        if item in allowed_dirs:
            removed.append(item)
        else:
            kept.append(item)

    if kept:
        settings["skillsDirectories"] = kept
    elif removed:
        del settings["skillsDirectories"]

    return {"removed": removed}


def make_backup(
    settings_path: pathlib.Path,
    backup_root: pathlib.Path,
) -> dict[str, Any]:
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_dir = backup_root / stamp
    counter = 0
    while backup_dir.exists():
        counter += 1
        backup_dir = backup_root / f"{stamp}.{counter}"
    backup_dir.mkdir(parents=True)

    backup_file = backup_dir / "settings.json"
    info: dict[str, Any] = {
        "backup_dir": str(backup_dir),
        "backup_path": None,
        "target_was_missing": not settings_path.exists(),
    }
    if settings_path.exists():
        shutil.copy2(settings_path, backup_file)
        info["backup_path"] = str(backup_file)
    else:
        marker = backup_dir / "settings.json.absent"
        marker.write_text(f"missing: {settings_path}\n", encoding="utf-8")
        info["absence_marker"] = str(marker)
    return info


def validate_temp_json(path: pathlib.Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MergeError(f"temporary settings JSON validation failed: {exc}") from exc
    if not isinstance(data, dict):
        raise MergeError("temporary settings JSON top-level value is not an object")


def atomic_write_settings(
    settings_path: pathlib.Path,
    text: str,
    expected_raw: bytes | None,
) -> None:
    try:
        current_raw = settings_path.read_bytes()
    except FileNotFoundError:
        current_raw = None
    if current_raw != expected_raw:
        raise MergeError("settings changed during merge; aborting without rewrite")

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{settings_path.name}.",
        suffix=".tmp",
        dir=str(settings_path.parent),
        text=True,
    )
    tmp_path = pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        validate_temp_json(tmp_path)
        os.replace(tmp_path, settings_path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: pathlib.Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    json.loads(tmp.read_text(encoding="utf-8"))
    os.replace(tmp, path)


def print_diff(settings_path: pathlib.Path, before: str, after: str) -> None:
    if before == after:
        print("No settings changes needed.")
        return
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=str(settings_path),
        tofile=f"{settings_path} (proposed)",
    )
    sys.stdout.writelines(diff)


def build_result(
    operation: str,
    settings_path: pathlib.Path,
    hooks_dir: pathlib.Path,
    skills_dir: pathlib.Path | None,
    changed: bool,
    backup: dict[str, Any] | None,
    before_raw: bytes | None,
    after_text: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "operation": operation,
        "settings_path": str(settings_path),
        "hooks_dir": str(hooks_dir),
        "skills_dir": str(skills_dir) if skills_dir else None,
        "changed": changed,
        "backup": backup,
        "before_sha256": sha256_bytes(before_raw),
        "after_sha256": hashlib.sha256(after_text.encode("utf-8")).hexdigest(),
    }
    result.update(details)
    if operation == "merge":
        result["entries"] = details.get("added_entries", [])
    return result


def run() -> int:
    args = parse_args()
    settings_path = pathlib.Path(args.settings).expanduser()
    hooks_dir = pathlib.Path(args.hooks_dir).expanduser()
    bin_dir = pathlib.Path(args.bin_dir).expanduser() if args.bin_dir else None
    skills_dir = pathlib.Path(args.skills_dir).expanduser() if args.skills_dir else None
    backup_dir = pathlib.Path(args.backup_dir).expanduser()
    manifest_path = pathlib.Path(args.manifest).expanduser() if args.manifest else None

    if not args.remove and not args.template:
        raise MergeError("--template is required unless --remove is used")

    original, before_raw = read_settings(settings_path)
    before_text = dump_settings(original)

    if args.remove:
        allowed_keys = manifest_entry_keys(manifest_path)
        allowed_skills_dirs = manifest_skills_dirs(manifest_path)
        if allowed_skills_dirs is None:
            allowed_skills_dirs = {skills_dir_key(skills_dir)} if skills_dir else set()
        merged, details = remove_agentstack_entries(original, hooks_dir, allowed_keys)
        details["skills_dirs"] = remove_skills_directories(merged, allowed_skills_dirs)
        details["permissions"] = {
            "removed": remove_permissions(merged, manifest_permissions(manifest_path))
        }
        operation = "remove"
    else:
        template_path = pathlib.Path(args.template).expanduser()
        template = render_template(template_path, hooks_dir, bin_dir)
        template_hooks = load_template_hooks(template, template_path)
        merged, details = merge_template_entries(original, template_hooks)
        details["skills_dirs"] = (
            merge_skills_directory(merged, skills_dir)
            if skills_dir
            else {"added": [], "skipped_existing": []}
        )
        details["permissions"] = merge_permissions(
            merged, load_template_permissions(template, template_path)
        )
        operation = "merge"

    after_text = dump_settings(merged)
    changed = before_text != after_text

    if args.dry_run:
        print_diff(settings_path, before_text, after_text)
        return 0

    backup = None
    if changed:
        backup = make_backup(settings_path, backup_dir)
        atomic_write_settings(settings_path, after_text, before_raw)

    result = build_result(
        operation,
        settings_path,
        hooks_dir,
        skills_dir,
        changed,
        backup,
        before_raw,
        after_text,
        details,
    )
    if args.result_json:
        atomic_write_json(pathlib.Path(args.result_json).expanduser(), result)
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


def main() -> int:
    try:
        return run()
    except MergeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
