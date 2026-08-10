"""Journaled compare-and-swap switch for copied MCP consumer settings.

The helper never discovers a user's home directory.  ``prepare`` receives an
explicit JSON inventory, renders every after-image without changing a target,
and seals exact before/after bytes in a private bundle.  ``apply`` and
``rollback`` require the manifest digest printed by ``prepare``.  Publication
is necessarily a sequence of same-directory atomic replacements: a crash may
leave a mixed vector, but that vector is never marked COMMITTED and one
``rollback`` invocation resumes it back to the exact before-images.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

from .contract import COMPATIBILITY_TOOLS, LOCAL_ONLY_TOOLS
from .migration import ASSESSABLE_STAGES, MigrationError, assess_rollback

try:  # pragma: no cover - Python 3.10 uses the declared tomli dependency.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


MANIFEST_NAME: Final[str] = "manifest.json"
JOURNAL_NAME: Final[str] = "journal.json"
COMMITTED_RECEIPT_NAME: Final[str] = "committed.json"
ROLLED_BACK_RECEIPT_NAME: Final[str] = "rolled-back.json"
SCHEMA_VERSION: Final[int] = 1
OLD_CLAUDE_KEY: Final[str] = "mcp-agent-mail"
OLD_CODEX_KEY: Final[str] = "agent-mail"
NEW_KEY: Final[str] = "agentstack-mail"
NEW_TOOLS: Final[frozenset[str]] = COMPATIBILITY_TOOLS
PROXY_TOOLS: Final[frozenset[str]] = LOCAL_ONLY_TOOLS | frozenset(
    {"acknowledge_message", "fetch_inbox", "send_message"}
)
SUPPORTED_KINDS: Final[frozenset[str]] = frozenset(
    {
        "claude_mcp",
        "claude_settings",
        "codex_mcp",
        "agentstack_env",
        "agentstack_state",
        "codex_app_env",
        "codex_app_state",
        "claude_child_mcp",
        "codex_child_mcp",
    }
)


class ConsumerError(RuntimeError):
    """A consumer setting or transactional safety check failed."""


FaultHook = Callable[[str], None]
AuthorityCheck = Callable[[], None]


@dataclass(frozen=True, slots=True)
class Desired:
    legacy_mcp_url: str
    new_mcp_url: str
    legacy_mail_db: str
    new_mail_db: str
    legacy_mail_env: str
    new_mail_env: str
    legacy_mail_home: str
    new_mail_home: str
    legacy_signals_dir: str
    new_signals_dir: str

    @classmethod
    def from_payload(cls, value: Any) -> Desired:
        if not isinstance(value, dict) or set(value) != {
            "legacy_mcp_url",
            "new_mcp_url",
            "legacy_mail_db",
            "new_mail_db",
            "legacy_mail_env",
            "new_mail_env",
            "legacy_mail_home",
            "new_mail_home",
            "legacy_signals_dir",
            "new_signals_dir",
        }:
            raise ConsumerError("inventory desired object has missing or extra fields")
        if not all(isinstance(item, str) and item for item in value.values()):
            raise ConsumerError("every desired value must be a non-empty string")
        desired = cls(**value)
        old = urlparse(desired.legacy_mcp_url)
        new = urlparse(desired.new_mcp_url)
        if (
            old.scheme != "http"
            or old.hostname not in {"127.0.0.1", "localhost", "::1"}
            or old.port != 8765
        ):
            raise ConsumerError("legacy_mcp_url must be loopback port 8765")
        if (
            new.scheme != "http"
            or new.hostname not in {"127.0.0.1", "localhost", "::1"}
            or new.port != 18765
            or new.path.rstrip("/") != "/mcp"
        ):
            raise ConsumerError("new_mcp_url must be loopback port 18765 /mcp")
        path_fields = {
            "legacy_mail_db": desired.legacy_mail_db,
            "new_mail_db": desired.new_mail_db,
            "legacy_mail_env": desired.legacy_mail_env,
            "new_mail_env": desired.new_mail_env,
            "legacy_mail_home": desired.legacy_mail_home,
            "new_mail_home": desired.new_mail_home,
            "legacy_signals_dir": desired.legacy_signals_dir,
            "new_signals_dir": desired.new_signals_dir,
        }
        if any(not Path(path).is_absolute() for path in path_fields.values()):
            raise ConsumerError("every legacy/new mail path must be absolute")
        if desired.legacy_mcp_url == desired.new_mcp_url:
            raise ConsumerError("legacy and new MCP endpoints must be distinct")
        for legacy, new in (
            (desired.legacy_mail_db, desired.new_mail_db),
            (desired.legacy_mail_env, desired.new_mail_env),
            (desired.legacy_mail_home, desired.new_mail_home),
            (desired.legacy_signals_dir, desired.new_signals_dir),
        ):
            if Path(legacy) == Path(new):
                raise ConsumerError("legacy and new mail paths must be distinct")
        new_home = Path(desired.new_mail_home)
        if Path(desired.new_mail_db) != new_home / "storage.sqlite3":
            raise ConsumerError("new_mail_db must be new_mail_home/storage.sqlite3")
        if Path(desired.new_signals_dir) != new_home / "signals":
            raise ConsumerError("new_signals_dir must be new_mail_home/signals")
        return desired

    def old_urls(self) -> frozenset[str]:
        parsed = urlparse(self.legacy_mcp_url)
        bases = {
            f"{parsed.scheme}://127.0.0.1:{parsed.port}",
            f"{parsed.scheme}://localhost:{parsed.port}",
            f"{parsed.scheme}://[::1]:{parsed.port}",
        }
        return frozenset(
            f"{base}{path}"
            for base in bases
            for path in ("/mcp", "/mcp/", "/api", "/api/")
        )


@dataclass(frozen=True, slots=True)
class Target:
    kind: str
    path: Path


@dataclass(frozen=True, slots=True)
class Snapshot:
    payload: bytes
    mode: int
    uid: int
    gid: int
    device: int
    inode: int
    flags: int
    xattrs: tuple[tuple[str, bytes], ...]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConsumerError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _load_json(raw: bytes, description: str) -> tuple[Any, bool]:
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_duplicate_rejecting_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConsumerError(f"{description} is not strict UTF-8 JSON: {exc}") from exc
    return value, text.endswith("\n")


def _render_json(value: Any, terminal_newline: bool) -> bytes:
    text = json.dumps(value, indent=2, ensure_ascii=False)
    if terminal_newline:
        text += "\n"
    return text.encode("utf-8")


def _require_lossless_json(raw: bytes, value: Any, terminal_newline: bool, name: str) -> None:
    if _render_json(value, terminal_newline) != raw:
        raise ConsumerError(
            f"{name} JSON formatting is unsupported; refusing whole-file reformat"
        )


def _rename_mapping_key(mapping: dict[str, Any], old: str, new: str) -> dict[str, Any]:
    if old not in mapping:
        return dict(mapping)
    if new in mapping:
        raise ConsumerError(f"both old and new MCP keys are present: {old}, {new}")
    return {new if key == old else key: value for key, value in mapping.items()}


def _validate_direct_entry(entry: Any, desired: Desired, *, old: bool) -> None:
    if not isinstance(entry, dict):
        raise ConsumerError("direct Claude MCP entry must be an object")
    allowed = {"type", "url", "headers"} if old else {"type", "url"}
    if set(entry) - allowed:
        raise ConsumerError("direct Claude MCP entry has unsupported transport fields")
    if entry.get("type") != "http":
        raise ConsumerError("direct Claude MCP entry must use HTTP")
    expected_urls = desired.old_urls() if old else frozenset({desired.new_mcp_url})
    if entry.get("url") not in expected_urls:
        raise ConsumerError("direct Claude MCP entry points at an unexpected endpoint")
    if old and "headers" in entry:
        headers = entry["headers"]
        if not isinstance(headers, dict) or set(headers) != {"Authorization"}:
            raise ConsumerError("legacy Claude MCP entry has unknown headers")


def _transform_claude_scope(scope: Any, desired: Desired) -> tuple[Any, bool]:
    if scope is None:
        return scope, False
    if not isinstance(scope, dict):
        raise ConsumerError("mcpServers must be an object")
    for name, entry in scope.items():
        if name not in {OLD_CLAUDE_KEY, NEW_KEY} and isinstance(entry, dict):
            if entry.get("url") in desired.old_urls():
                raise ConsumerError(f"undeclared Claude legacy endpoint alias: {name}")
    if OLD_CLAUDE_KEY in scope and NEW_KEY in scope:
        raise ConsumerError("Claude scope contains both old and new MCP keys")
    if OLD_CLAUDE_KEY in scope:
        _validate_direct_entry(scope[OLD_CLAUDE_KEY], desired, old=True)
        renamed = _rename_mapping_key(scope, OLD_CLAUDE_KEY, NEW_KEY)
        renamed[NEW_KEY] = {"type": "http", "url": desired.new_mcp_url}
        return renamed, True
    if NEW_KEY in scope:
        _validate_direct_entry(scope[NEW_KEY], desired, old=False)
        return dict(scope), True
    return dict(scope), False


def _transform_claude_mcp(raw: bytes, desired: Desired) -> bytes:
    value, newline = _load_json(raw, "Claude MCP config")
    if not isinstance(value, dict):
        raise ConsumerError("Claude MCP config top-level must be an object")
    _require_lossless_json(raw, value, newline, "Claude MCP config")
    result = dict(value)
    found = False
    servers, changed = _transform_claude_scope(value.get("mcpServers"), desired)
    if servers is not None:
        result["mcpServers"] = servers
    found |= changed
    projects = value.get("projects")
    if projects is not None:
        if not isinstance(projects, dict):
            raise ConsumerError("Claude projects must be an object")
        new_projects: dict[str, Any] = {}
        for project, payload in projects.items():
            if not isinstance(payload, dict):
                new_projects[project] = payload
                continue
            project_payload = dict(payload)
            scoped, scoped_found = _transform_claude_scope(
                payload.get("mcpServers"), desired
            )
            if scoped is not None:
                project_payload["mcpServers"] = scoped
            found |= scoped_found
            new_projects[project] = project_payload
        result["projects"] = new_projects
    if not found:
        raise ConsumerError("Claude config has no declared old or new MCP entry")
    return _render_json(result, newline)


def _transform_permission(value: str, *, deny: bool) -> str | None:
    old_prefix = f"mcp__{OLD_CLAUDE_KEY}__"
    new_prefix = f"mcp__{NEW_KEY}__"
    if not value.startswith(old_prefix):
        if value.startswith(new_prefix):
            tool = value[len(new_prefix) :]
            if tool not in NEW_TOOLS:
                raise ConsumerError(f"new Claude permission names non-contract tool: {tool}")
        return value
    tool = value[len(old_prefix) :]
    if deny and tool not in NEW_TOOLS:
        raise ConsumerError(
            f"legacy-only deny rule requires an explicit operator decision: {tool}"
        )
    return new_prefix + tool if tool in NEW_TOOLS else None


def _transform_claude_settings(raw: bytes, desired: Desired) -> bytes:
    del desired
    value, newline = _load_json(raw, "Claude settings")
    if not isinstance(value, dict):
        raise ConsumerError("Claude settings top-level must be an object")
    _require_lossless_json(raw, value, newline, "Claude settings")
    result = json.loads(json.dumps(value))
    declared_selector = False
    old_prefix = f"mcp__{OLD_CLAUDE_KEY}__"
    new_prefix = f"mcp__{NEW_KEY}__"
    permissions = result.get("permissions")
    if permissions is not None:
        if not isinstance(permissions, dict):
            raise ConsumerError("Claude permissions must be an object")
        for field in ("allow", "deny"):
            entries = permissions.get(field)
            if entries is None:
                continue
            if not isinstance(entries, list) or not all(isinstance(x, str) for x in entries):
                raise ConsumerError(f"Claude permissions.{field} must be a string list")
            transformed: list[str] = []
            for entry in entries:
                if entry.startswith((old_prefix, new_prefix)):
                    declared_selector = True
                replacement = _transform_permission(entry, deny=field == "deny")
                if replacement is not None:
                    if replacement in transformed:
                        raise ConsumerError("permission rename would create a duplicate")
                    transformed.append(replacement)
            permissions[field] = transformed
    hooks = result.get("hooks")
    if hooks is not None:
        if not isinstance(hooks, dict):
            raise ConsumerError("Claude hooks must be an object")
        for groups in hooks.values():
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                matcher = group.get("matcher")
                if isinstance(matcher, str) and matcher.startswith(new_prefix):
                    declared_selector = True
                    if matcher[len(new_prefix) :] not in NEW_TOOLS:
                        raise ConsumerError("new Claude hook matcher names a non-contract tool")
                if isinstance(matcher, str) and old_prefix in matcher:
                    declared_selector = True
                    if not matcher.startswith(old_prefix) or matcher.count(old_prefix) != 1:
                        raise ConsumerError("compound legacy hook matcher is unsupported")
                    tool = matcher[len(old_prefix) :]
                    if tool not in NEW_TOOLS:
                        raise ConsumerError("legacy-only hook matcher cannot be translated")
                    group["matcher"] = new_prefix + tool
    rendered = _render_json(result, newline)
    if old_prefix.encode() in rendered:
        raise ConsumerError("legacy Claude tool prefix remains outside known selectors")
    if not declared_selector:
        raise ConsumerError("Claude settings has no declared old or new tool selector")
    return rendered


def _toml_path(header_line: str) -> tuple[str, ...]:
    stripped = header_line.strip()
    if not (stripped.startswith("[") and stripped.endswith("]") and not stripped.startswith("[[")):
        raise ConsumerError(f"unsupported TOML table header: {header_line!r}")
    marker = "__agentstack_consumer_marker__"
    try:
        value = tomllib.loads(f"{stripped}\n{marker} = true\n")
    except tomllib.TOMLDecodeError as exc:
        raise ConsumerError(f"invalid TOML table header: {stripped}: {exc}") from exc
    path: list[str] = []
    current: Any = value
    while isinstance(current, dict) and marker not in current:
        if len(current) != 1:
            raise ConsumerError(f"ambiguous TOML table header: {stripped}")
        key, current = next(iter(current.items()))
        path.append(key)
    if not isinstance(current, dict) or current.get(marker) is not True:
        raise ConsumerError(f"could not parse TOML table header: {stripped}")
    return tuple(path)


_TOML_STRING_ASSIGNMENT = re.compile(
    r'^(?P<prefix>\s*(?:"(?P<quoted_key>(?:\\.|[^"])*)"|(?P<bare_key>[A-Za-z0-9_-]+))\s*=\s*)'
    r'(?P<value>"(?:\\.|[^"])*")(?P<suffix>\s*(?:#.*)?)(?P<newline>\r?\n?)$'
)


def _toml_assignment(line: str) -> tuple[str, str, re.Match[str]] | None:
    match = _TOML_STRING_ASSIGNMENT.match(line)
    if not match:
        return None
    key = match.group("quoted_key") or match.group("bare_key") or ""
    try:
        value = tomllib.loads(f"x = {match.group('value')}\n")["x"]
    except tomllib.TOMLDecodeError as exc:
        raise ConsumerError(f"invalid TOML string assignment: {line!r}") from exc
    return key, value, match


def _toml_header(path: tuple[str, ...]) -> str:
    def segment(value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_-]+", value):
            return value
        return json.dumps(value, ensure_ascii=False)

    return "[" + ".".join(segment(item) for item in path) + "]"


def _transform_codex_mcp(raw: bytes, desired: Desired, *, child: bool = False) -> bytes:
    try:
        text = raw.decode("utf-8")
        parsed = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConsumerError(f"Codex config is not valid UTF-8 TOML: {exc}") from exc
    servers = parsed.get("mcp_servers")
    if not isinstance(servers, dict):
        raise ConsumerError("Codex config has no mcp_servers table")
    if OLD_CODEX_KEY in servers and NEW_KEY in servers:
        raise ConsumerError("Codex config contains both old and new MCP keys")
    old_urls = desired.old_urls()
    for name, entry in servers.items():
        if name not in {OLD_CODEX_KEY, NEW_KEY} and isinstance(entry, dict):
            if entry.get("url") in old_urls:
                raise ConsumerError(f"undeclared Codex legacy endpoint alias: {name}")
    source_key = OLD_CODEX_KEY if OLD_CODEX_KEY in servers else NEW_KEY if NEW_KEY in servers else None
    if source_key is None:
        raise ConsumerError("Codex config has no declared old or new MCP entry")
    entry = servers[source_key]
    if not isinstance(entry, dict):
        raise ConsumerError("Codex MCP server entry must be a table")
    if child:
        if not isinstance(entry.get("command"), str):
            raise ConsumerError("Codex child MCP entry is not a stdio proxy")
        if set(entry) - {"command", "args", "env", "tools"}:
            raise ConsumerError("Codex child MCP entry has mixed or unknown transport fields")
        if not isinstance(entry.get("args"), list) or not isinstance(entry.get("env"), dict):
            raise ConsumerError("Codex child MCP entry lacks args or env")
    else:
        if set(entry) - {"url", "bearer_token_env_var", "tools"}:
            raise ConsumerError("Codex direct MCP entry has mixed or unknown transport fields")
        expected = old_urls if source_key == OLD_CODEX_KEY else {desired.new_mcp_url}
        if entry.get("url") not in expected:
            raise ConsumerError("Codex MCP entry points at an unexpected endpoint")
        bearer = entry.get("bearer_token_env_var")
        if source_key == OLD_CODEX_KEY and bearer not in {None, "MCP_AGENT_MAIL_TOKEN"}:
            raise ConsumerError("Codex direct MCP entry uses an unknown bearer selector")
        if source_key == NEW_KEY and bearer is not None:
            raise ConsumerError("new Codex MCP entry still carries bearer auth")
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    current: tuple[str, ...] | None = None
    saw_root = False
    saw_url = False
    child_env_seen: set[str] = set()
    tool_surface = PROXY_TOOLS if child else NEW_TOOLS
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            current = _toml_path(stripped)
            if current[:2] == ("mcp_servers", source_key):
                if len(current) >= 4 and current[2] == "tools" and current[3] not in tool_surface:
                    current = ("__drop__",)
                    continue
                renamed = ("mcp_servers", NEW_KEY, *current[2:])
                if len(current) == 2:
                    saw_root = True
                newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                output.append(_toml_header(renamed) + newline)
                current = renamed
                continue
        if current == ("__drop__",):
            continue
        if current == ("mcp_servers", NEW_KEY):
            assignment = _toml_assignment(line)
            if assignment:
                key, value, match = assignment
                if key == "url":
                    if child:
                        raise ConsumerError("Codex child proxy unexpectedly contains direct url")
                    expected = old_urls if source_key == OLD_CODEX_KEY else {desired.new_mcp_url}
                    if value not in expected:
                        raise ConsumerError("Codex root url changed during render")
                    replacement = json.dumps(desired.new_mcp_url)
                    line = (
                        match.group("prefix")
                        + replacement
                        + match.group("suffix")
                        + match.group("newline")
                    )
                    saw_url = True
                elif key == "bearer_token_env_var":
                    if source_key == NEW_KEY:
                        raise ConsumerError("new Codex MCP entry still carries bearer auth")
                    continue
        if child and current == ("mcp_servers", NEW_KEY, "env"):
            assignment = _toml_assignment(line)
            if assignment:
                key, value, match = assignment
                replacements = {
                    "AGENTSTACK_MCP_URL": (desired.legacy_mcp_url, desired.new_mcp_url),
                    "AGENTSTACK_MAIL_ENV": (desired.legacy_mail_env, desired.new_mail_env),
                }
                if key in replacements:
                    old, new = replacements[key]
                    allowed = (
                        set(desired.old_urls()) | {new}
                        if key == "AGENTSTACK_MCP_URL"
                        else {old, new}
                    )
                    if value not in allowed:
                        raise ConsumerError(f"Codex child {key} has an unexpected value")
                    if key in child_env_seen:
                        raise ConsumerError(f"Codex child repeats {key}")
                    child_env_seen.add(key)
                    line = (
                        match.group("prefix")
                        + json.dumps(new)
                        + match.group("suffix")
                        + match.group("newline")
                    )
        output.append(line)
    if not saw_root:
        raise ConsumerError("Codex MCP root table was not found exactly once")
    if not child and not saw_url:
        raise ConsumerError("Codex MCP root table has no supported url assignment")
    if child and child_env_seen != {"AGENTSTACK_MCP_URL", "AGENTSTACK_MAIL_ENV"}:
        raise ConsumerError("Codex child proxy lacks explicit endpoint or mail env")
    rendered = "".join(output).encode("utf-8")
    try:
        reparsed = tomllib.loads(rendered.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConsumerError(f"rendered Codex config is invalid: {exc}") from exc
    new_entry = reparsed.get("mcp_servers", {}).get(NEW_KEY)
    if not isinstance(new_entry, dict):
        raise ConsumerError("rendered Codex config lacks the new MCP key")
    if not child and new_entry.get("url") != desired.new_mcp_url:
        raise ConsumerError("rendered Codex config lacks the new endpoint")
    if "bearer_token_env_var" in new_entry:
        raise ConsumerError("rendered Codex config retained bearer auth")
    return rendered


_ENV_ASSIGNMENT = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=)(?P<value>[^\r\n]*)(?P<newline>\r?\n?)$"
)


def _transform_env(raw: bytes, desired: Desired, *, codex_app: bool) -> bytes:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConsumerError(f"environment file is not UTF-8: {exc}") from exc
    replacements = {
        "AGENTSTACK_MCP_URL": (desired.legacy_mcp_url, desired.new_mcp_url),
        "AGENTSTACK_MAIL_ENV": (desired.legacy_mail_env, desired.new_mail_env),
        "AGENTSTACK_SIGNALS_DIR": (
            desired.legacy_signals_dir,
            desired.new_signals_dir,
        ),
    }
    if not codex_app:
        replacements.update(
            {
                "AGENTSTACK_MAIL_DB": (desired.legacy_mail_db, desired.new_mail_db),
                "AGENTSTACK_MAIL_HOME": (
                    desired.legacy_mail_home,
                    desired.new_mail_home,
                ),
            }
        )
    seen: set[str] = set()
    output: list[str] = []
    for line in text.splitlines(keepends=True):
        match = _ENV_ASSIGNMENT.match(line)
        if not match or match.group("key") not in replacements:
            output.append(line)
            continue
        key = match.group("key")
        if key in seen:
            raise ConsumerError(f"duplicate environment assignment: {key}")
        seen.add(key)
        try:
            parts = shlex.split(match.group("value"), posix=True)
        except ValueError as exc:
            raise ConsumerError(f"unsupported shell value for {key}: {exc}") from exc
        if len(parts) != 1:
            raise ConsumerError(f"unsupported shell value for {key}")
        old, new = replacements[key]
        allowed = (
            set(desired.old_urls()) | {new}
            if key == "AGENTSTACK_MCP_URL"
            else {old, new}
        )
        if parts[0] not in allowed:
            raise ConsumerError(f"{key} has an unexpected value")
        output.append(match.group("prefix") + shlex.quote(new) + match.group("newline"))
    missing = set(replacements) - seen
    if missing:
        raise ConsumerError(f"environment file is missing selectors: {sorted(missing)}")
    return "".join(output).encode("utf-8")


def _transform_state(raw: bytes, desired: Desired, *, codex_app: bool) -> bytes:
    value, newline = _load_json(raw, "AgentStack install state")
    if not isinstance(value, dict):
        raise ConsumerError("AgentStack install state top-level must be an object")
    _require_lossless_json(raw, value, newline, "AgentStack install state")
    result = json.loads(json.dumps(value))
    if codex_app:
        target = result
        replacements = {
            "agent_mail_url": (desired.legacy_mcp_url, desired.new_mcp_url),
            "agent_mail_env": (desired.legacy_mail_env, desired.new_mail_env),
            "signals_dir": (desired.legacy_signals_dir, desired.new_signals_dir),
        }
    else:
        target = result.get("env")
        if not isinstance(target, dict):
            raise ConsumerError("AgentStack install state env must be an object")
        replacements = {
            "AGENTSTACK_MCP_URL": (desired.legacy_mcp_url, desired.new_mcp_url),
            "AGENTSTACK_MAIL_DB": (desired.legacy_mail_db, desired.new_mail_db),
            "AGENTSTACK_MAIL_ENV": (desired.legacy_mail_env, desired.new_mail_env),
            "AGENTSTACK_MAIL_HOME": (desired.legacy_mail_home, desired.new_mail_home),
            "AGENTSTACK_SIGNALS_DIR": (
                desired.legacy_signals_dir,
                desired.new_signals_dir,
            ),
        }
    for key, (old, new) in replacements.items():
        allowed = (
            set(desired.old_urls()) | {new}
            if key in {"AGENTSTACK_MCP_URL", "agent_mail_url"}
            else {old, new}
        )
        if target.get(key) not in allowed:
            raise ConsumerError(f"install state selector {key} has an unexpected value")
        target[key] = new
    if not codex_app:
        purge_paths = result.get("purge_paths")
        if purge_paths is not None:
            if not isinstance(purge_paths, list) or not all(
                isinstance(item, str) and Path(item).is_absolute()
                for item in purge_paths
            ):
                raise ConsumerError("install state purge_paths must be absolute paths")
            legacy_roots = {
                str(Path(desired.legacy_mail_home)),
                str(Path(desired.legacy_mail_db).parent),
            }
            result["purge_paths"] = [
                item for item in purge_paths if str(Path(item)) not in legacy_roots
            ]
            if desired.new_mail_home not in result["purge_paths"]:
                result["purge_paths"].append(desired.new_mail_home)
        retained_paths = result.get("retained_paths")
        if retained_paths is not None:
            if not isinstance(retained_paths, list) or not all(
                isinstance(item, str) and Path(item).is_absolute()
                for item in retained_paths
            ):
                raise ConsumerError("install state retained_paths must be absolute paths")
            for path in (
                desired.legacy_mail_home,
                str(Path(desired.legacy_mail_db).parent),
                desired.new_mail_home,
            ):
                if path not in retained_paths:
                    retained_paths.append(path)
    return _render_json(result, newline)


def _transform_claude_child(raw: bytes, desired: Desired) -> bytes:
    value, newline = _load_json(raw, "Claude child MCP config")
    if not isinstance(value, dict):
        raise ConsumerError("Claude child MCP config top-level must be an object")
    _require_lossless_json(raw, value, newline, "Claude child MCP config")
    servers = value.get("mcpServers")
    if not isinstance(servers, dict):
        raise ConsumerError("Claude child MCP config has no mcpServers object")
    if OLD_CLAUDE_KEY in servers and NEW_KEY in servers:
        raise ConsumerError("Claude child has both old and new MCP keys")
    source = OLD_CLAUDE_KEY if OLD_CLAUDE_KEY in servers else NEW_KEY if NEW_KEY in servers else None
    if source is None:
        raise ConsumerError("Claude child has no declared old or new MCP key")
    entry = servers[source]
    if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
        raise ConsumerError("Claude child MCP entry is not a stdio proxy")
    env = entry.get("env")
    if not isinstance(env, dict):
        raise ConsumerError("Claude child MCP entry has no env object")
    for key, old, new in (
        ("AGENTSTACK_MCP_URL", desired.legacy_mcp_url, desired.new_mcp_url),
        ("AGENTSTACK_MAIL_ENV", desired.legacy_mail_env, desired.new_mail_env),
    ):
        allowed = (
            set(desired.old_urls()) | {new}
            if key == "AGENTSTACK_MCP_URL"
            else {old, new}
        )
        if env.get(key) not in allowed:
            raise ConsumerError(f"Claude child {key} has an unexpected value")
        env[key] = new
    result = dict(value)
    result["mcpServers"] = (
        dict(servers)
        if source == NEW_KEY
        else _rename_mapping_key(servers, source, NEW_KEY)
    )
    result["mcpServers"][NEW_KEY] = entry
    return _render_json(result, newline)


TRANSFORMS: Final[Mapping[str, Callable[[bytes, Desired], bytes]]] = {
    "claude_mcp": _transform_claude_mcp,
    "claude_settings": _transform_claude_settings,
    "codex_mcp": _transform_codex_mcp,
    "agentstack_env": lambda raw, desired: _transform_env(raw, desired, codex_app=False),
    "agentstack_state": lambda raw, desired: _transform_state(raw, desired, codex_app=False),
    "codex_app_env": lambda raw, desired: _transform_env(raw, desired, codex_app=True),
    "codex_app_state": lambda raw, desired: _transform_state(raw, desired, codex_app=True),
    "claude_child_mcp": _transform_claude_child,
    "codex_child_mcp": lambda raw, desired: _transform_codex_mcp(raw, desired, child=True),
}


def _assert_no_symlink_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise ConsumerError(f"cannot inspect target ancestor {current}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ConsumerError(f"target ancestor is not a real directory: {current}")


def _xattr_names(path: Path) -> list[str]:
    if hasattr(os, "listxattr"):
        return sorted(os.listxattr(path, follow_symlinks=False))  # type: ignore[attr-defined]
    executable = Path("/usr/bin/xattr")
    if sys.platform != "darwin" or not executable.is_file():
        return []
    result = subprocess.run(
        [str(executable), str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ConsumerError(f"cannot list extended attributes for {path}")
    return sorted(line for line in result.stdout.splitlines() if line)


def _read_xattrs(path: Path) -> tuple[tuple[str, bytes], ...]:
    values: list[tuple[str, bytes]] = []
    for name in _xattr_names(path):
        if "\n" in name or "\x00" in name:
            raise ConsumerError(f"unsupported extended attribute name on {path}")
        if hasattr(os, "getxattr"):
            payload = os.getxattr(path, name, follow_symlinks=False)  # type: ignore[attr-defined]
        else:
            result = subprocess.run(
                ["/usr/bin/xattr", "-px", name, str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise ConsumerError(f"cannot read extended attribute {name!r} on {path}")
            try:
                payload = bytes.fromhex("".join(result.stdout.split()))
            except ValueError as exc:
                raise ConsumerError(f"invalid xattr hex output for {name!r}") from exc
        values.append((name, payload))
    return tuple(values)


def _write_xattrs(path: Path, values: tuple[tuple[str, bytes], ...]) -> None:
    desired = dict(values)
    for name in _xattr_names(path):
        if name in desired:
            continue
        if hasattr(os, "removexattr"):
            os.removexattr(path, name, follow_symlinks=False)  # type: ignore[attr-defined]
        else:
            result = subprocess.run(
                ["/usr/bin/xattr", "-d", name, str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if result.returncode != 0:
                raise ConsumerError(f"cannot remove extended attribute {name!r}")
    for name, payload in values:
        if hasattr(os, "setxattr"):
            os.setxattr(path, name, payload, follow_symlinks=False)  # type: ignore[attr-defined]
        else:
            result = subprocess.run(
                ["/usr/bin/xattr", "-wx", name, payload.hex(), str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if result.returncode != 0:
                raise ConsumerError(f"cannot write extended attribute {name!r}")


def _reject_acl(path: Path) -> None:
    if sys.platform != "darwin":
        return
    result = subprocess.run(
        ["/bin/ls", "-lde", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ConsumerError(f"cannot inspect ACL metadata for {path}")
    if len(result.stdout.splitlines()) > 1:
        raise ConsumerError(f"consumer target has an ACL, which is unsupported: {path}")


def _snapshot(path: Path) -> Snapshot:
    if not path.is_absolute():
        raise ConsumerError(f"consumer path must be absolute: {path}")
    _assert_no_symlink_ancestors(path)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ConsumerError(f"cannot inspect consumer target {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ConsumerError(f"consumer target is not a regular file: {path}")
    if info.st_nlink != 1:
        raise ConsumerError(f"consumer target has hard links: {path}")
    _reject_acl(path)
    xattrs_before = _read_xattrs(path)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ConsumerError(f"cannot read consumer target {path}: {exc}") from exc
    xattrs_after = _read_xattrs(path)
    after = os.lstat(path)
    if (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        stat.S_IMODE(info.st_mode),
        info.st_uid,
        info.st_gid,
        getattr(info, "st_flags", 0),
        xattrs_before,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        stat.S_IMODE(after.st_mode),
        after.st_uid,
        after.st_gid,
        getattr(after, "st_flags", 0),
        xattrs_after,
    ):
        raise ConsumerError(f"consumer target changed while being read: {path}")
    return Snapshot(
        payload=payload,
        mode=stat.S_IMODE(info.st_mode),
        uid=info.st_uid,
        gid=info.st_gid,
        device=info.st_dev,
        inode=info.st_ino,
        flags=getattr(info, "st_flags", 0),
        xattrs=xattrs_before,
    )


def _load_inventory(path: Path) -> tuple[Desired, list[Target]]:
    value, _ = _load_json(path.read_bytes(), "consumer inventory")
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "desired",
        "consumers",
    }:
        raise ConsumerError("inventory has missing or extra top-level fields")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ConsumerError("unsupported consumer inventory schema")
    desired = Desired.from_payload(value["desired"])
    consumers = value["consumers"]
    if not isinstance(consumers, list) or not consumers:
        raise ConsumerError("consumer inventory must be a non-empty list")
    targets: list[Target] = []
    path_keys: set[str] = set()
    for item in consumers:
        if not isinstance(item, dict) or set(item) != {"kind", "path"}:
            raise ConsumerError("each consumer must contain exactly kind and path")
        kind, raw_path = item["kind"], item["path"]
        if kind not in SUPPORTED_KINDS or not isinstance(raw_path, str):
            raise ConsumerError("consumer has unsupported kind or path")
        target_path = Path(raw_path)
        if not target_path.is_absolute():
            raise ConsumerError(f"consumer path must be absolute: {target_path}")
        key = unicodedata.normalize("NFD", str(target_path)).casefold()
        if key in path_keys:
            raise ConsumerError(f"duplicate/case/Unicode consumer alias: {target_path}")
        path_keys.add(key)
        targets.append(Target(kind=kind, path=target_path))
    return desired, targets


def _write_private(path: Path, payload: bytes, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare(inventory: Path, bundle: Path) -> dict[str, Any]:
    if bundle.exists() or bundle.is_symlink():
        raise ConsumerError(f"bundle path already exists: {bundle}")
    desired, targets = _load_inventory(inventory)
    snapshots: list[tuple[Target, Snapshot, bytes]] = []
    identities: set[tuple[int, int]] = set()
    for target in targets:
        before = _snapshot(target.path)
        identity = (before.device, before.inode)
        if identity in identities:
            raise ConsumerError("two consumer paths resolve to the same inode")
        identities.add(identity)
        after = TRANSFORMS[target.kind](before.payload, desired)
        if not after:
            raise ConsumerError(f"rendered empty consumer file: {target.path}")
        snapshots.append((target, before, after))
    bundle.mkdir(mode=0o700)
    operation_id = str(uuid.uuid4())
    entries: list[dict[str, Any]] = []
    try:
        for index, (target, before, after) in enumerate(snapshots):
            before_name = f"{index:04d}.before"
            after_name = f"{index:04d}.after"
            _write_private(bundle / before_name, before.payload)
            _write_private(bundle / after_name, after)
            entries.append(
                {
                    "kind": target.kind,
                    "path": str(target.path),
                    "mode": before.mode,
                    "uid": before.uid,
                    "gid": before.gid,
                    "flags": before.flags,
                    "xattrs": [
                        {"name": name, "value_base64": base64.b64encode(value).decode("ascii")}
                        for name, value in before.xattrs
                    ],
                    "before_blob": before_name,
                    "before_sha256": _sha256(before.payload),
                    "after_blob": after_name,
                    "after_sha256": _sha256(after),
                }
            )
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "tool": "agentstack-mail-consumers",
            "operation_id": operation_id,
            "entries": entries,
        }
        manifest_bytes = _canonical_json(manifest) + b"\n"
        _write_private(bundle / MANIFEST_NAME, manifest_bytes)
        _write_private(
            bundle / JOURNAL_NAME,
            _canonical_json(
                {
                    "schema_version": SCHEMA_VERSION,
                    "operation_id": operation_id,
                    "phase": "PREPARED",
                }
            )
            + b"\n",
        )
        for path in bundle.iterdir():
            if path.name != JOURNAL_NAME:
                os.chmod(path, 0o400)
        _fsync_directory(bundle)
        _fsync_directory(bundle.parent)
    except Exception:
        for path in bundle.iterdir() if bundle.exists() else ():
            path.unlink(missing_ok=True)
        bundle.rmdir()
        raise
    return {
        "status": "prepared",
        "bundle": str(bundle),
        "operation_id": operation_id,
        "manifest_sha256": _sha256(manifest_bytes),
        "consumer_count": len(entries),
        "targets_changed": sum(
            before.payload != after for _, before, after in snapshots
        ),
    }


def _load_bundle(bundle: Path, expected_digest: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise ConsumerError("expected manifest digest must be 64 lowercase hex characters")
    manifest_path = bundle / MANIFEST_NAME
    raw = _read_immutable_bundle_file(manifest_path)
    if _sha256(raw) != expected_digest:
        raise ConsumerError("manifest digest does not match the external pin")
    value, _ = _load_json(raw, "consumer manifest")
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("tool") != "agentstack-mail-consumers"
        or not isinstance(value.get("operation_id"), str)
        or not isinstance(value.get("entries"), list)
    ):
        raise ConsumerError("consumer manifest is malformed")
    try:
        parsed_operation_id = uuid.UUID(value["operation_id"])
    except (ValueError, AttributeError) as exc:
        raise ConsumerError("consumer manifest operation_id is not a UUID") from exc
    if str(parsed_operation_id) != value["operation_id"]:
        raise ConsumerError("consumer manifest operation_id is not canonical")
    entries: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    seen_blobs: set[str] = set()
    for entry in value["entries"]:
        required = {
            "kind",
            "path",
            "mode",
            "uid",
            "gid",
            "flags",
            "xattrs",
            "before_blob",
            "before_sha256",
            "after_blob",
            "after_sha256",
        }
        if not isinstance(entry, dict) or set(entry) != required:
            raise ConsumerError("consumer manifest entry is malformed")
        if entry["kind"] not in SUPPORTED_KINDS:
            raise ConsumerError("consumer manifest has unsupported kind")
        path = Path(entry["path"])
        path_key = unicodedata.normalize("NFD", str(path)).casefold()
        if not path.is_absolute() or path_key in seen_paths:
            raise ConsumerError("consumer manifest has duplicate or relative path")
        if path == bundle or bundle in path.parents:
            raise ConsumerError("consumer target overlaps its rollback bundle")
        seen_paths.add(path_key)
        if (
            not isinstance(entry["mode"], int)
            or not 0 <= entry["mode"] <= 0o7777
            or not isinstance(entry["uid"], int)
            or not isinstance(entry["gid"], int)
            or not isinstance(entry["flags"], int)
            or not isinstance(entry["xattrs"], list)
        ):
            raise ConsumerError("consumer manifest metadata is malformed")
        decoded_xattrs: list[tuple[str, bytes]] = []
        seen_xattrs: set[str] = set()
        for item in entry["xattrs"]:
            if not isinstance(item, dict) or set(item) != {"name", "value_base64"}:
                raise ConsumerError("consumer manifest xattr is malformed")
            name, encoded = item["name"], item["value_base64"]
            if not isinstance(name, str) or not isinstance(encoded, str) or name in seen_xattrs:
                raise ConsumerError("consumer manifest xattr is duplicate or malformed")
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ConsumerError("consumer manifest xattr is not valid base64") from exc
            seen_xattrs.add(name)
            decoded_xattrs.append((name, decoded))
        entry = dict(entry)
        entry["decoded_xattrs"] = tuple(decoded_xattrs)
        for field, digest_field in (
            ("before_blob", "before_sha256"),
            ("after_blob", "after_sha256"),
        ):
            name = entry[field]
            if (
                not isinstance(name, str)
                or Path(name).name != name
                or name in seen_blobs
            ):
                raise ConsumerError("consumer manifest has unsafe or duplicate blob")
            seen_blobs.add(name)
            payload = _read_immutable_bundle_file(bundle / name)
            if _sha256(payload) != entry[digest_field]:
                raise ConsumerError(f"consumer bundle blob failed digest: {name}")
        entries.append(entry)
    if not entries:
        raise ConsumerError("consumer manifest has no entries")
    return value, entries


def _read_immutable_bundle_file(path: Path) -> bytes:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ConsumerError(f"cannot inspect consumer bundle file {path.name}: {exc}") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o400
    ):
        raise ConsumerError(f"consumer bundle file is not immutable regular data: {path.name}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ConsumerError(f"cannot read consumer bundle file {path.name}: {exc}") from exc


def _classify(bundle: Path, entry: Mapping[str, Any]) -> str:
    current = _snapshot(Path(entry["path"]))
    if (current.mode, current.uid, current.gid, current.flags, current.xattrs) != (
        entry["mode"],
        entry["uid"],
        entry["gid"],
        entry["flags"],
        entry["decoded_xattrs"],
    ):
        return "third"
    digest = _sha256(current.payload)
    before = entry["before_sha256"]
    after = entry["after_sha256"]
    if before == after and digest == before:
        return "both"
    if digest == before:
        return "before"
    if digest == after:
        return "after"
    return "third"


def _classify_all(bundle: Path, entries: Sequence[Mapping[str, Any]]) -> list[str]:
    return [_classify(bundle, entry) for entry in entries]


def _write_journal(bundle: Path, operation_id: str, phase: str) -> None:
    payload = _canonical_json(
        {
            "schema_version": SCHEMA_VERSION,
            "operation_id": operation_id,
            "phase": phase,
        }
    ) + b"\n"
    destination = bundle / JOURNAL_NAME
    descriptor, name = tempfile.mkstemp(prefix=".journal.", dir=bundle)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        _fsync_directory(bundle)
    finally:
        temporary.unlink(missing_ok=True)


def _journal_phase(bundle: Path, operation_id: str) -> str:
    try:
        value, _ = _load_json((bundle / JOURNAL_NAME).read_bytes(), "consumer journal")
    except (OSError, ConsumerError):
        return "INVALID"
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("operation_id") != operation_id
        or value.get("phase")
        not in {"PREPARED", "PUBLISHING", "COMMITTED", "ROLLING_BACK", "ROLLED_BACK"}
    ):
        return "INVALID"
    return str(value["phase"])


def _stage_path(
    entry: Mapping[str, Any],
    *,
    operation_id: str,
    index: int,
    field: str,
    state: str = "ready",
) -> Path:
    target = Path(entry["path"])
    return target.parent / (
        f".agentstack-consumer.{operation_id}.{index:04d}.{field}.{state}"
    )


def _verify_staged(
    temporary: Path, entry: Mapping[str, Any], *, field: str
) -> None:
    staged = _snapshot(temporary)
    if (
        _sha256(staged.payload) != entry[f"{field}_sha256"]
        or (staged.mode, staged.uid, staged.gid, staged.flags, staged.xattrs)
        != (
            entry["mode"],
            entry["uid"],
            entry["gid"],
            entry["flags"],
            entry["decoded_xattrs"],
        )
    ):
        raise ConsumerError(f"staged {field} image failed verification")


def _stage(
    entry: Mapping[str, Any],
    bundle: Path,
    field: str,
    *,
    operation_id: str,
    index: int,
) -> Path:
    target = Path(entry["path"])
    payload = _read_immutable_bundle_file(bundle / entry[f"{field}_blob"])
    ready = _stage_path(
        entry, operation_id=operation_id, index=index, field=field
    )
    building = _stage_path(
        entry,
        operation_id=operation_id,
        index=index,
        field=field,
        state="building",
    )
    if ready.exists() or ready.is_symlink():
        _verify_staged(ready, entry, field=field)
        return ready
    if building.exists() or building.is_symlink():
        info = os.lstat(building)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
        ):
            raise ConsumerError("unsafe partial consumer stage blocks crash recovery")
        building.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(building, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(building, int(entry["mode"]))
        os.chown(building, int(entry["uid"]), int(entry["gid"]))
        if int(entry["flags"]):
            if not hasattr(os, "chflags"):
                raise ConsumerError("target flags cannot be preserved on this platform")
            os.chflags(building, int(entry["flags"]), follow_symlinks=False)  # type: ignore[attr-defined]
        _write_xattrs(building, entry["decoded_xattrs"])
        with building.open("rb") as handle:
            os.fsync(handle.fileno())
        _verify_staged(building, entry, field=field)
        os.replace(building, ready)
        _fsync_directory(target.parent)
        _verify_staged(ready, entry, field=field)
        return ready
    except Exception:
        building.unlink(missing_ok=True)
        raise


def _cleanup_stages(
    entries: Sequence[Mapping[str, Any]], *, operation_id: str
) -> None:
    parents: set[Path] = set()
    for index, entry in enumerate(entries):
        for field in ("before", "after"):
            for state in ("building", "ready"):
                path = _stage_path(
                    entry,
                    operation_id=operation_id,
                    index=index,
                    field=field,
                    state=state,
                )
                try:
                    info = os.lstat(path)
                except FileNotFoundError:
                    continue
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid != os.getuid()
                ):
                    raise ConsumerError(f"unsafe consumer stage cannot be cleaned: {path}")
                if state == "ready":
                    _verify_staged(path, entry, field=field)
                path.unlink()
                parents.add(path.parent)
    for parent in parents:
        _fsync_directory(parent)


def _call_fault(hook: FaultHook | None, phase: str) -> None:
    if hook is not None:
        hook(phase)


def _publish(
    bundle: Path,
    entries: Sequence[Mapping[str, Any]],
    *,
    operation_id: str,
    direction: str,
    fault_hook: FaultHook | None,
    pre_publish_check: AuthorityCheck | None = None,
) -> None:
    source = "before" if direction == "apply" else "after"
    destination = "after" if direction == "apply" else "before"
    states = _classify_all(bundle, entries)
    if "third" in states:
        raise ConsumerError("consumer target diverged; refusing every replacement")
    staged: dict[int, Path] = {}
    try:
        for index, (entry, state) in enumerate(zip(entries, states, strict=True)):
            if state in {destination, "both"}:
                continue
            if state != source:
                raise ConsumerError("consumer vector is incompatible with requested operation")
            _call_fault(fault_hook, f"before_stage:{index}")
            staged[index] = _stage(
                entry,
                bundle,
                destination,
                operation_id=operation_id,
                index=index,
            )
            _call_fault(fault_hook, f"after_stage:{index}")
        if "third" in _classify_all(bundle, entries):
            raise ConsumerError("consumer changed after staging; no target was replaced")
        if pre_publish_check is not None:
            pre_publish_check()
        phase = "PUBLISHING" if direction == "apply" else "ROLLING_BACK"
        _write_journal(bundle, operation_id, phase)
        _call_fault(fault_hook, f"after_journal:{phase}")
        for index, entry in enumerate(entries):
            if index not in staged:
                continue
            _call_fault(fault_hook, f"before_replace:{index}")
            _verify_staged(staged[index], entry, field=destination)
            current = _classify(bundle, entry)
            if current != source:
                raise ConsumerError(
                    f"consumer changed before replacement: {entry['path']}"
                )
            os.replace(staged[index], Path(entry["path"]))
            staged.pop(index)
            _call_fault(fault_hook, f"after_replace:{index}")
            _fsync_directory(Path(entry["path"]).parent)
            _call_fault(fault_hook, f"after_parent_fsync:{index}")
            if _classify(bundle, entry) not in {destination, "both"}:
                raise ConsumerError("published consumer image failed verification")
        final = _classify_all(bundle, entries)
        if any(state not in {destination, "both"} for state in final):
            raise ConsumerError("consumer vector did not converge")
        _cleanup_stages(entries, operation_id=operation_id)
        for parent in {Path(entry["path"]).parent for entry in entries}:
            _fsync_directory(parent)
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def _with_lock(bundle: Path):
    lock = (bundle / ".lock").open("a+b")
    os.chmod(bundle / ".lock", 0o600)
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise ConsumerError("another consumer operation holds the bundle lock") from exc
    return lock


@contextmanager
def _with_target_locks(entries: Sequence[Mapping[str, Any]]):
    """Serialize helpers by stable target-parent inode across file replacement."""

    descriptors: list[int] = []
    try:
        parents = sorted({Path(entry["path"]).parent for entry in entries}, key=str)
        for parent in parents:
            _assert_no_symlink_ancestors(parent / "placeholder")
            descriptor = os.open(parent, os.O_RDONLY)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(descriptor)
                raise ConsumerError(
                    f"another consumer operation holds a target-directory lock: {parent}"
                ) from exc
            descriptors.append(descriptor)
        yield
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def _with_authority_lock(migration_manifest: Path):
    """Fence the normal new-service writer from rollback assessment to receipt."""

    try:
        payload, _ = _load_json(
            migration_manifest.read_bytes(), "migration authority manifest"
        )
    except OSError as exc:
        raise ConsumerError(f"cannot read migration authority manifest: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(
        payload.get("destination_root"), str
    ):
        raise ConsumerError("migration authority manifest lacks destination_root")
    destination_root = Path(payload["destination_root"])
    if (
        not destination_root.is_absolute()
        or migration_manifest.parent != destination_root
    ):
        raise ConsumerError("migration authority manifest is not inside destination_root")
    lock_path = destination_root / "runtime" / "authority.lock"
    _assert_no_symlink_ancestors(lock_path)
    try:
        info = os.lstat(lock_path)
    except OSError as exc:
        raise ConsumerError(
            "new service authority lock is absent; C4 service start/stop is unproven"
        ) from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_nlink != 1
    ):
        raise ConsumerError("new service authority lock is not a regular file")
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags)
    handle = os.fdopen(descriptor, "r+b", closefd=True)
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConsumerError(
                "new service still owns the authority lock; refusing config rollback"
            ) from exc
        yield
    finally:
        handle.close()


def _receipt_payload(
    operation_id: str, manifest_digest: str, phase: str
) -> bytes:
    return _canonical_json(
        {
            "schema_version": SCHEMA_VERSION,
            "operation_id": operation_id,
            "manifest_sha256": manifest_digest,
            "phase": phase,
        }
    ) + b"\n"


def _receipt_path(bundle: Path, phase: str) -> Path:
    if phase == "COMMITTED":
        return bundle / COMMITTED_RECEIPT_NAME
    if phase == "ROLLED_BACK":
        return bundle / ROLLED_BACK_RECEIPT_NAME
    raise ConsumerError(f"unsupported receipt phase: {phase}")


def _has_valid_receipt(
    bundle: Path, operation_id: str, manifest_digest: str, phase: str
) -> bool:
    path = _receipt_path(bundle, phase)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    payload = _read_immutable_bundle_file(path)
    if payload != _receipt_payload(operation_id, manifest_digest, phase):
        raise ConsumerError(f"consumer {phase.lower()} receipt is invalid")
    return True


def _write_receipt(
    bundle: Path, operation_id: str, manifest_digest: str, phase: str
) -> None:
    path = _receipt_path(bundle, phase)
    payload = _receipt_payload(operation_id, manifest_digest, phase)
    if _has_valid_receipt(bundle, operation_id, manifest_digest, phase):
        return
    pending = bundle / f".{path.name}.{operation_id}.pending"
    if pending.exists() or pending.is_symlink():
        try:
            pending_payload = _read_immutable_bundle_file(pending)
        except ConsumerError:
            info = os.lstat(pending)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
            ):
                raise ConsumerError("unsafe partial terminal receipt blocks recovery")
            pending.unlink()
        else:
            if pending_payload != payload:
                pending.unlink()
    if not pending.exists():
        _write_private(pending, payload, mode=0o400)
    try:
        os.replace(pending, path)
    except FileExistsError:
        if not _has_valid_receipt(bundle, operation_id, manifest_digest, phase):
            raise ConsumerError(f"consumer {phase.lower()} receipt raced")
    _fsync_directory(bundle)


def _assert_data_reversible(migration_manifest: Path, cutover_stage: str) -> None:
    try:
        assessment = assess_rollback(migration_manifest, cutover_stage)
    except (MigrationError, OSError) as exc:
        raise ConsumerError(f"cannot establish rollback authority baseline: {exc}") from exc
    if (
        assessment.get("status") != "reversible"
        or assessment.get("data_reversible") is not True
    ):
        raise ConsumerError(
            "migration rollback assessment is no-go: "
            + str(assessment.get("reason", "unknown reason"))
        )


def apply(
    bundle: Path, expected_digest: str, *, fault_hook: FaultHook | None = None
) -> dict[str, Any]:
    manifest, entries = _load_bundle(bundle, expected_digest)
    with _with_lock(bundle):
        with _with_target_locks(entries):
            _publish(
                bundle,
                entries,
                operation_id=manifest["operation_id"],
                direction="apply",
                fault_hook=fault_hook,
            )
            _write_receipt(
                bundle, manifest["operation_id"], expected_digest, "COMMITTED"
            )
            _write_journal(bundle, manifest["operation_id"], "COMMITTED")
    return status(bundle, expected_digest)


def rollback(
    bundle: Path,
    expected_digest: str,
    migration_manifest: Path,
    cutover_stage: str,
    *,
    fault_hook: FaultHook | None = None,
) -> dict[str, Any]:
    manifest, entries = _load_bundle(bundle, expected_digest)
    authority_check = lambda: _assert_data_reversible(
        migration_manifest, cutover_stage
    )
    with _with_lock(bundle):
        with _with_authority_lock(migration_manifest):
            with _with_target_locks(entries):
                authority_check()
                _publish(
                    bundle,
                    entries,
                    operation_id=manifest["operation_id"],
                    direction="rollback",
                    fault_hook=fault_hook,
                    pre_publish_check=authority_check,
                )
                _write_receipt(
                    bundle, manifest["operation_id"], expected_digest, "ROLLED_BACK"
                )
                _write_journal(bundle, manifest["operation_id"], "ROLLED_BACK")
    return status(bundle, expected_digest)


def status(bundle: Path, expected_digest: str) -> dict[str, Any]:
    manifest, entries = _load_bundle(bundle, expected_digest)
    states = _classify_all(bundle, entries)
    phase = _journal_phase(bundle, manifest["operation_id"])
    receipt_invalid = False
    try:
        committed_receipt = _has_valid_receipt(
            bundle, manifest["operation_id"], expected_digest, "COMMITTED"
        )
        rolled_back_receipt = _has_valid_receipt(
            bundle, manifest["operation_id"], expected_digest, "ROLLED_BACK"
        )
    except ConsumerError:
        committed_receipt = False
        rolled_back_receipt = False
        receipt_invalid = True
    if "third" in states or receipt_invalid:
        state = "incident"
    elif all(item in {"after", "both"} for item in states):
        state = "committed" if committed_receipt else "all_after_uncommitted"
    elif all(item in {"before", "both"} for item in states):
        if rolled_back_receipt:
            state = "rolled_back"
        elif phase == "PREPARED":
            state = "prepared"
        else:
            state = "all_before_uncommitted"
    else:
        state = "mixed_uncommitted"
    return {
        "status": state,
        "journal_phase": phase,
        "committed_receipt": committed_receipt,
        "rolled_back_receipt": rolled_back_receipt,
        "receipt_invalid": receipt_invalid,
        "operation_id": manifest["operation_id"],
        "consumer_count": len(entries),
        "before": states.count("before"),
        "after": states.count("after"),
        "unchanged": states.count("both"),
        "third": states.count("third"),
    }


def preview(bundle: Path, expected_digest: str) -> dict[str, Any]:
    """Return secret-free file and line ranges changed by the sealed plan."""

    manifest, entries = _load_bundle(bundle, expected_digest)
    changes: list[dict[str, Any]] = []
    for entry in entries:
        before = _read_immutable_bundle_file(bundle / entry["before_blob"]).splitlines()
        after = _read_immutable_bundle_file(bundle / entry["after_blob"]).splitlines()
        hunks: list[dict[str, int]] = []
        for tag, first_start, first_end, second_start, second_end in difflib.SequenceMatcher(
            a=before, b=after, autojunk=False
        ).get_opcodes():
            if tag == "equal":
                continue
            hunks.append(
                {
                    "before_start": first_start + 1,
                    "before_lines": first_end - first_start,
                    "after_start": second_start + 1,
                    "after_lines": second_end - second_start,
                }
            )
        changes.append(
            {
                "path": entry["path"],
                "kind": entry["kind"],
                "changed": entry["before_sha256"] != entry["after_sha256"],
                "hunks": hunks,
            }
        )
    return {
        "status": "preview",
        "operation_id": manifest["operation_id"],
        "consumer_count": len(entries),
        "files_changed": sum(item["changed"] for item in changes),
        "changes": changes,
        "contents_redacted": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentstack-mail-consumers")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--inventory", required=True)
    prepare_parser.add_argument("--bundle", required=True)
    for command in ("preview", "apply", "status", "rollback"):
        operation = subparsers.add_parser(command)
        operation.add_argument("--bundle", required=True)
        operation.add_argument("--expected-manifest-sha256", required=True)
        if command == "rollback":
            operation.add_argument("--migration-manifest", required=True)
            operation.add_argument(
                "--cutover-stage", required=True, choices=ASSESSABLE_STAGES
            )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        bundle = Path(args.bundle)
        if args.command == "prepare":
            result = prepare(Path(args.inventory), bundle)
        elif args.command == "preview":
            result = preview(bundle, args.expected_manifest_sha256)
        elif args.command == "apply":
            result = apply(bundle, args.expected_manifest_sha256)
        elif args.command == "rollback":
            result = rollback(
                bundle,
                args.expected_manifest_sha256,
                Path(args.migration_manifest),
                args.cutover_stage,
            )
        else:
            result = status(bundle, args.expected_manifest_sha256)
    except (ConsumerError, OSError) as exc:
        print(f"agentstack-mail-consumers: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result.get("status") in {
        "incident",
        "mixed_uncommitted",
        "all_before_uncommitted",
        "all_after_uncommitted",
    }:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
