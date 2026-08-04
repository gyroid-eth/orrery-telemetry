#!/usr/bin/env python3
"""Safely register claude-agent-stack's fixed Claude Code MCP server."""

from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any


# In the repo this file sits beside mcp_endpoint.py in scripts/lib/; once
# installed it is bin/agentstack-merge-claude-mcp with the module under
# bin/lib/. Look in both rather than assuming one layout.
_HERE = pathlib.Path(__file__).resolve().parent
for _candidate in (_HERE, _HERE / "lib"):
    if (_candidate / "mcp_endpoint.py").is_file():
        sys.path.insert(0, str(_candidate))
        break

from mcp_endpoint import INTERCHANGEABLE_MCP_PATHS, same_endpoint  # noqa: E402,F401


SERVER_NAME = "mcp-agent-mail"


class MergeError(RuntimeError):
    """Expected config problem that must not rewrite the user's file."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely add or remove the Claude Code agent-mail MCP entry."
    )
    parser.add_argument("--config", required=True, help="Claude Code user config")
    parser.add_argument("--mcp-url", help="agent-mail HTTP MCP endpoint")
    parser.add_argument("--mail-env", default="", help="file containing HTTP_BEARER_TOKEN")
    parser.add_argument("--backup-dir", required=True, help="backup root")
    parser.add_argument("--result-json", help="write a machine-readable result")
    parser.add_argument(
        "--existing-result",
        help="prior merge result whose original-user baseline should survive upgrades",
    )
    parser.add_argument("--manifest", help="install manifest used by --remove")
    parser.add_argument("--dry-run", action="store_true", help="preview without writing")
    parser.add_argument("--check", action="store_true", help="print configured or needs-merge")
    parser.add_argument("--remove", action="store_true", help="undo the manifest-recorded merge")
    return parser.parse_args()


def read_config(path: pathlib.Path) -> tuple[dict[str, Any], bytes | None]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, None
    if not raw.strip():
        return {}, raw
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MergeError(f"Claude config is not valid UTF-8 JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MergeError(f"Claude config top-level value must be an object: {path}")
    return data, raw


def mcp_servers(config: dict[str, Any], *, create: bool) -> dict[str, Any] | None:
    servers = config.get("mcpServers")
    if servers is None and create:
        servers = {}
        config["mcpServers"] = servers
    if servers is not None and not isinstance(servers, dict):
        raise MergeError("Claude config mcpServers must be an object; refusing to repair it")
    return servers


def read_bearer_token(path: pathlib.Path | None) -> str:
    if path is None:
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as exc:
        raise MergeError(f"could not read agent-mail env {path}: {exc}") from exc
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip() == "HTTP_BEARER_TOKEN":
            token = value.strip()
            if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
                token = token[1:-1]
            return token.strip()
    return ""


def desired_entry(mcp_url: str, token: str) -> dict[str, Any]:
    if not mcp_url:
        raise MergeError("--mcp-url is required unless --remove is used")
    entry: dict[str, Any] = {"type": "http", "url": mcp_url}
    if token:
        entry["headers"] = {"Authorization": f"Bearer {token}"}
    return entry


def already_reaches_server(existing: Any, desired: dict[str, Any]) -> bool:
    """True when the entry already works and rewriting it would only be churn.

    The point of registering the server is that delegation can reach it. An
    entry that already reaches it is not a problem to be corrected — rewriting
    a working `/api/` to `/mcp` changes nothing, risks the one thing that was
    working, and discards whatever reason the user had for writing it that way.
    Credentials are different: a stale token does not reach the server, so a
    header mismatch still needs the merge.
    """
    if not isinstance(existing, dict):
        return False
    if existing.get("type") != desired.get("type"):
        return False
    if not same_endpoint(str(existing.get("url", "")), str(desired.get("url", ""))):
        return False
    return existing.get("headers") == desired.get("headers")


def entry_hash(entry: Any) -> str:
    canonical = json.dumps(
        entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def preview_view(entry: Any) -> dict[str, Any]:
    if entry is None:
        return {"mcpServers": {}}
    safe = copy.deepcopy(entry)
    if isinstance(safe, dict) and isinstance(safe.get("headers"), dict):
        safe["headers"] = {key: "<redacted>" for key in safe["headers"]}
    return {"mcpServers": {SERVER_NAME: safe}}


def dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def print_preview(path: pathlib.Path, before: Any, after: Any) -> None:
    before_text = dump(preview_view(before))
    after_text = dump(preview_view(after))
    if before_text == after_text:
        print("No Claude MCP changes needed.")
        return
    diff = difflib.unified_diff(
        before_text.splitlines(keepends=True),
        after_text.splitlines(keepends=True),
        fromfile=f"{path} (current {SERVER_NAME} entry; secrets redacted)",
        tofile=f"{path} (proposed {SERVER_NAME} entry; secrets redacted)",
    )
    print("".join(diff), end="")


def make_backup(path: pathlib.Path, root: pathlib.Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = root / f"claude-mcp.{stamp}"
    suffix = 0
    while directory.exists():
        suffix += 1
        directory = root / f"claude-mcp.{stamp}.{suffix}"
    directory.mkdir(mode=0o700)
    result: dict[str, Any] = {
        "backup_dir": str(directory),
        "backup_path": None,
        "target_was_missing": not path.exists(),
    }
    if path.exists():
        backup = directory / "claude.json"
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
        result["backup_path"] = str(backup)
    else:
        marker = directory / "claude.json.absent"
        marker.write_text(f"missing: {path}\n", encoding="utf-8")
        os.chmod(marker, 0o600)
        result["absence_marker"] = str(marker)
    return result


def atomic_write(path: pathlib.Path, config: dict[str, Any], expected: bytes | None) -> None:
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        current = None
    if current != expected:
        raise MergeError("Claude config changed during merge; aborting without rewrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    tmp = pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        checked = json.loads(tmp.read_text(encoding="utf-8"))
        if not isinstance(checked, dict):
            raise MergeError("temporary Claude config top-level value is not an object")
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def write_result(path: pathlib.Path | None, result: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def merge(args: argparse.Namespace) -> dict[str, Any]:
    path = pathlib.Path(args.config).expanduser()
    original, raw = read_config(path)
    merged = copy.deepcopy(original)
    servers = mcp_servers(merged, create=True)
    assert servers is not None
    previous = copy.deepcopy(servers.get(SERVER_NAME))
    token_path = pathlib.Path(args.mail_env).expanduser() if args.mail_env else None
    installed = desired_entry(args.mcp_url or "", read_bearer_token(token_path))
    if already_reaches_server(previous, installed):
        # Keep the user's spelling. This is the "configured" answer, not a
        # smaller diff: there is nothing here to fix.
        installed = copy.deepcopy(previous)
    servers[SERVER_NAME] = installed
    changed = original != merged
    if args.check:
        print("needs-merge" if changed else "configured")
        return {
            "operation": "check",
            "config_path": str(path),
            "server_name": SERVER_NAME,
            "changed": changed,
        }
    print_preview(path, previous, installed)
    baseline_backup = None
    previous_entry_existed = previous is not None
    if args.existing_result and previous is not None:
        prior_path = pathlib.Path(args.existing_result).expanduser()
        try:
            prior = json.loads(prior_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            prior = None
        except (OSError, json.JSONDecodeError) as exc:
            raise MergeError(f"could not read prior Claude MCP merge result: {exc}") from exc
        if (
            isinstance(prior, dict)
            and prior.get("changed")
            and prior.get("installed_entry_sha256") == entry_hash(previous)
        ):
            previous_entry_existed = bool(prior.get("previous_entry_existed"))
            if isinstance(prior.get("backup"), dict):
                baseline_backup = prior["backup"]
    operation_backup = None
    if changed and not args.dry_run:
        operation_backup = make_backup(path, pathlib.Path(args.backup_dir).expanduser())
        atomic_write(path, merged, raw)
    backup = baseline_backup or operation_backup
    result = {
        "operation": "merge",
        "config_path": str(path),
        "server_name": SERVER_NAME,
        "changed": changed,
        "previous_entry_existed": previous_entry_existed,
        "installed_entry_sha256": entry_hash(installed),
        "backup": backup,
    }
    if baseline_backup is not None and operation_backup is not None:
        result["operation_backup"] = operation_backup
    if not args.dry_run:
        write_result(
            pathlib.Path(args.result_json).expanduser() if args.result_json else None,
            result,
        )
    return result


def remove(args: argparse.Namespace) -> dict[str, Any]:
    if not args.manifest:
        raise MergeError("--manifest is required with --remove")
    manifest_path = pathlib.Path(args.manifest).expanduser()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MergeError(f"could not read install manifest {manifest_path}: {exc}") from exc
    recorded = manifest.get("claude_mcp_merge")
    if not isinstance(recorded, dict) or not recorded.get("changed"):
        result = {"operation": "remove", "changed": False, "reason": "not-recorded"}
        print("No manifest-recorded Claude MCP change to remove.")
        return result
    raw_config_path = recorded.get("config_path")
    if not isinstance(raw_config_path, str) or not raw_config_path:
        raise MergeError("manifest Claude MCP merge has no config_path")
    path = pathlib.Path(raw_config_path).expanduser()
    original, raw = read_config(path)
    merged = copy.deepcopy(original)
    servers = mcp_servers(merged, create=False)
    current = servers.get(SERVER_NAME) if servers is not None else None
    if current is None or entry_hash(current) != recorded.get("installed_entry_sha256"):
        result = {
            "operation": "remove",
            "config_path": str(path),
            "server_name": SERVER_NAME,
            "changed": False,
            "kept_modified": current is not None,
        }
        print(f"Kept modified or absent Claude MCP entry: {SERVER_NAME}")
        return result

    restored = None
    if recorded.get("previous_entry_existed"):
        backup = recorded.get("backup")
        backup_path = backup.get("backup_path") if isinstance(backup, dict) else None
        if not isinstance(backup_path, str) or not backup_path:
            raise MergeError("manifest Claude MCP merge has no usable backup")
        backup_config, _ = read_config(pathlib.Path(backup_path).expanduser())
        backup_servers = mcp_servers(backup_config, create=False)
        if backup_servers is None or SERVER_NAME not in backup_servers:
            raise MergeError("Claude MCP backup has no previous fixed-name entry")
        restored = copy.deepcopy(backup_servers[SERVER_NAME])
        assert servers is not None
        servers[SERVER_NAME] = restored
    else:
        assert servers is not None
        del servers[SERVER_NAME]
        if not servers:
            del merged["mcpServers"]

    print_preview(path, current, restored)
    backup = None
    if not args.dry_run:
        backup = make_backup(path, pathlib.Path(args.backup_dir).expanduser())
        atomic_write(path, merged, raw)
    result = {
        "operation": "remove",
        "config_path": str(path),
        "server_name": SERVER_NAME,
        "changed": True,
        "restored_previous": restored is not None,
        "backup": backup,
    }
    if not args.dry_run:
        write_result(
            pathlib.Path(args.result_json).expanduser() if args.result_json else None,
            result,
        )
    return result


def main() -> int:
    args = parse_args()
    try:
        remove(args) if args.remove else merge(args)
    except MergeError as exc:
        print(f"error: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
