#!/usr/bin/env python3
"""Private Codex-child resume material and regenerated-home lifecycle.

The retained state contains an owner credential and is therefore deliberately
kept outside the dashboard API.  This module is shared by fresh child launches,
normal cleanup, dashboard resume, explicit purge, and expiry maintenance.
"""

from __future__ import annotations

import argparse
import fcntl
import hmac
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from datetime import date, datetime, time, timedelta, timezone
import tomllib
from typing import Any


SCHEMA_VERSION = 1
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
CODEX_PROGRAMS = {"codex", "codex-cli"}
MCP_PROFILES = {"inherit", "orrery-only"}
MAX_STATE_BYTES = 65536
MAX_TOKEN_BYTES = 4096


class ResumeStateError(ValueError):
    """A stable fail-closed reason suitable for dashboard capability output."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _utc_now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ResumeStateError("config_unrestorable", f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResumeStateError("config_unrestorable", f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ResumeStateError("config_unrestorable", f"{label} has no timezone")
    return parsed.astimezone(timezone.utc)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_private(path: Path, label: str, limit: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise ResumeStateError("credential_missing", f"{label} is missing") from exc
    except OSError as exc:
        raise ResumeStateError("credential_permission", f"{label} is unsafe") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ResumeStateError("credential_permission", f"{label} is unsafe")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ResumeStateError(
                "credential_permission", f"{label} permissions must be 0600"
            )
        raw = os.read(descriptor, limit + 1)
    finally:
        os.close(descriptor)
    if len(raw) > limit:
        raise ResumeStateError("credential_missing", f"{label} is too large")
    return raw


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read_private(path, "Codex child state", MAX_STATE_BYTES))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResumeStateError(
            "config_unrestorable", "Codex child state is invalid"
        ) from exc
    if not isinstance(value, dict):
        raise ResumeStateError("config_unrestorable", "Codex child state is invalid")
    return value


def _paths(runtime_dir: Path, agent_name: str) -> tuple[Path, Path, Path, Path, Path]:
    if not SAFE_NAME.fullmatch(agent_name):
        raise ResumeStateError("invalid_identity", "Codex child identity is unsafe")
    state_dir = runtime_dir / "child-agents"
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", agent_name)
    return (
        state_dir / f"{agent_name}.json",
        runtime_dir / f"agent_token_{key}",
        state_dir / f"{agent_name}.codex-home",
        state_dir / f"{agent_name}.mcp.json",
        state_dir / f".{agent_name}.resume.lock",
    )


class _AgentLock:
    def __init__(self, path: Path, *, exclusive: bool):
        self.path = path
        self.exclusive = exclusive
        self.descriptor: int | None = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self.descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(self.descriptor, 0o600)
        fcntl.flock(
            self.descriptor, fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
        )
        return self

    def __exit__(self, *_args):
        if self.descriptor is not None:
            os.close(self.descriptor)


def _validate_identity(
    state: dict[str, Any],
    *,
    agent_name: str,
    agent_id: int | None = None,
    project_key: str | None = None,
    program: str | None = None,
) -> None:
    actual_program = state.get("program")
    if (
        type(state.get("agent_id")) is not int
        or state["agent_id"] <= 0
        or state.get("agent_name") != agent_name
        or not isinstance(state.get("project_key"), str)
        or not state["project_key"]
        or not isinstance(actual_program, str)
        or actual_program not in CODEX_PROGRAMS
        or (agent_id is not None and state["agent_id"] != agent_id)
        or (project_key is not None and state["project_key"] != project_key)
        or (
            program is not None
            and (not isinstance(program, str) or program not in CODEX_PROGRAMS)
        )
    ):
        raise ResumeStateError(
            "identity_mismatch", "Codex child state belongs to another registration"
        )


def prepare_active_state(
    runtime_dir: Path, agent_name: str, *, project_key: str, mcp_profile: str
) -> dict[str, Any]:
    state_path, token_path, _home, _mcp, lock_path = _paths(runtime_dir, agent_name)
    if mcp_profile not in MCP_PROFILES:
        raise ResumeStateError("config_unrestorable", "invalid Codex MCP profile")
    with _AgentLock(lock_path, exclusive=True):
        state = _load_state(state_path)
        _validate_identity(state, agent_name=agent_name, project_key=project_key)
        state_token = state.get("registration_token")
        try:
            canonical = _read_private(
                token_path, "canonical Codex child credential", MAX_TOKEN_BYTES
            ).decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ResumeStateError(
                "credential_missing", "canonical Codex child credential is invalid"
            ) from exc
        if (
            not isinstance(state_token, str)
            or not state_token
            or len(state_token) > MAX_TOKEN_BYTES
            or not canonical
            or not hmac.compare_digest(
                canonical.encode("utf-8"), state_token.encode("utf-8")
            )
        ):
            raise ResumeStateError(
                "identity_mismatch",
                "Codex child state and credential are from different registrations",
            )
        state.update(
            schema_version=SCHEMA_VERSION,
            launch_origin="child",
            provider="codex",
            codex_mcp_profile=mcp_profile,
            retired_at=None,
            resume_expires_at=None,
        )
        state.pop("resume_in_progress_at", None)
        _atomic_json(state_path, state)
        tombstone = runtime_dir / "child-resume-tombstones" / f"{state['agent_id']}.json"
        tombstone.unlink(missing_ok=True)
        return state


def mark_retired(
    runtime_dir: Path,
    agent_name: str,
    *,
    retention_days: int,
    now: datetime | None = None,
) -> bool:
    """Mark a valid Codex child retained. Return False for legacy/non-Codex state."""

    if retention_days < 0:
        raise ValueError("retention days must be a non-negative integer")
    if retention_days == 0:
        return False
    state_path, token_path, _home, _mcp, lock_path = _paths(runtime_dir, agent_name)
    with _AgentLock(lock_path, exclusive=True):
        try:
            state = _load_state(state_path)
            _validate_identity(state, agent_name=agent_name)
        except ResumeStateError:
            return False
        if (
            state.get("schema_version") != SCHEMA_VERSION
            or state.get("launch_origin") != "child"
            or state.get("provider") != "codex"
            or not isinstance(state.get("codex_mcp_profile"), str)
            or state.get("codex_mcp_profile") not in MCP_PROFILES
        ):
            return False
        state_token = state.get("registration_token")
        try:
            canonical = _read_private(
                token_path, "canonical Codex child credential", MAX_TOKEN_BYTES
            ).decode("utf-8").strip()
        except (ResumeStateError, UnicodeDecodeError):
            return False
        if (
            not isinstance(state_token, str)
            or not state_token
            or not hmac.compare_digest(
                canonical.encode("utf-8"), state_token.encode("utf-8")
            )
        ):
            return False
        retired = _utc_now(now)
        state["retired_at"] = _timestamp(retired)
        state["resume_expires_at"] = _timestamp(
            retired + timedelta(days=retention_days)
        )
        state.pop("resume_in_progress_at", None)
        _atomic_json(state_path, state)
        os.chmod(token_path, 0o600)
        return True


def begin_resume(
    runtime_dir: Path,
    agent_name: str,
    *,
    agent_id: int,
    project_key: str,
    program: str,
    mcp_profile: str,
    now: datetime | None = None,
) -> None:
    """Protect a validated retained credential from expiry while Codex runs."""

    state_path, token_path, _home, _mcp, lock_path = _paths(runtime_dir, agent_name)
    with _AgentLock(lock_path, exclusive=True):
        state = _load_state(state_path)
        _validate_identity(
            state,
            agent_name=agent_name,
            agent_id=agent_id,
            project_key=project_key,
            program=program,
        )
        if (
            state.get("schema_version") != SCHEMA_VERSION
            or state.get("launch_origin") != "child"
            or state.get("provider") != "codex"
            or state.get("codex_mcp_profile") != mcp_profile
            or mcp_profile not in MCP_PROFILES
            or state.get("resume_in_progress_at") is not None
        ):
            raise ResumeStateError(
                "config_unrestorable", "Codex child resume state is not ready"
            )
        _parse_timestamp(state.get("retired_at"), "retired_at")
        expires_at = _parse_timestamp(state.get("resume_expires_at"), "resume_expires_at")
        current = _utc_now(now)
        if current >= expires_at:
            raise ResumeStateError(
                "retention_expired", "Codex child resume retention has expired"
            )
        state_token = state.get("registration_token")
        try:
            canonical = _read_private(
                token_path, "canonical Codex child credential", MAX_TOKEN_BYTES
            ).decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ResumeStateError(
                "credential_missing", "canonical Codex child credential is invalid"
            ) from exc
        if not isinstance(state_token, str) or not state_token or not canonical:
            raise ResumeStateError(
                "credential_missing", "retained Codex child credential is unavailable"
            )
        if not hmac.compare_digest(
            canonical.encode("utf-8"), state_token.encode("utf-8")
        ):
            raise ResumeStateError(
                "identity_mismatch",
                "Codex child state and credential are from different registrations",
            )
        state["resume_in_progress_at"] = _timestamp(current)
        _atomic_json(state_path, state)


def cancel_resume(
    runtime_dir: Path,
    agent_name: str,
    *,
    agent_id: int,
    project_key: str,
) -> None:
    """Restore the retained state when bootstrap fails before Codex exec."""

    state_path, _token, _home, _mcp, lock_path = _paths(runtime_dir, agent_name)
    with _AgentLock(lock_path, exclusive=True):
        state = _load_state(state_path)
        _validate_identity(
            state,
            agent_name=agent_name,
            agent_id=agent_id,
            project_key=project_key,
        )
        state.pop("resume_in_progress_at", None)
        _atomic_json(state_path, state)


def inspect_retained(
    runtime_dir: Path,
    agent_name: str,
    *,
    agent_id: int,
    project_key: str,
    program: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    state_path, token_path, _home, _mcp, lock_path = _paths(runtime_dir, agent_name)
    if not state_path.exists():
        tombstone_path = runtime_dir / "child-resume-tombstones" / f"{agent_id}.json"
        if tombstone_path.exists():
            try:
                tombstone = json.loads(
                    _read_private(tombstone_path, "child resume tombstone", 16384)
                )
            except (ResumeStateError, UnicodeDecodeError, json.JSONDecodeError):
                tombstone = None
            if (
                isinstance(tombstone, dict)
                and tombstone.get("agent_id") == agent_id
                and tombstone.get("agent_name") == agent_name
                and tombstone.get("project_key") == project_key
                and tombstone.get("reason") in {"purged", "retention_expired"}
            ):
                raise ResumeStateError(tombstone["reason"], "resume material removed")
        raise ResumeStateError(
            "credential_missing", "retained Codex child state is unavailable"
        )
    with _AgentLock(lock_path, exclusive=False):
        state = _load_state(state_path)
        _validate_identity(
            state,
            agent_name=agent_name,
            agent_id=agent_id,
            project_key=project_key,
            program=program,
        )
        if (
            state.get("schema_version") != SCHEMA_VERSION
            or state.get("launch_origin") != "child"
            or state.get("provider") != "codex"
            or not isinstance(state.get("codex_mcp_profile"), str)
            or state.get("codex_mcp_profile") not in MCP_PROFILES
        ):
            raise ResumeStateError(
                "config_unrestorable", "Codex child resume state is incomplete"
            )
        if state.get("resume_in_progress_at") is not None:
            raise ResumeStateError(
                "config_unrestorable", "Codex child resume is already in progress"
            )
        _parse_timestamp(state.get("retired_at"), "retired_at")
        expires_at = _parse_timestamp(state.get("resume_expires_at"), "resume_expires_at")
        if _utc_now(now) >= expires_at:
            raise ResumeStateError(
                "retention_expired", "Codex child resume retention has expired"
            )
        state_token = state.get("registration_token")
        try:
            canonical = _read_private(
                token_path, "canonical Codex child credential", MAX_TOKEN_BYTES
            ).decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ResumeStateError(
                "credential_missing", "canonical Codex child credential is invalid"
            ) from exc
        if not isinstance(state_token, str) or not state_token or not canonical:
            raise ResumeStateError(
                "credential_missing", "retained Codex child credential is unavailable"
            )
        if not hmac.compare_digest(
            canonical.encode("utf-8"), state_token.encode("utf-8")
        ):
            raise ResumeStateError(
                "identity_mismatch",
                "Codex child state and credential are from different registrations",
            )
        return state


def _remove_exact(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def purge_one(
    runtime_dir: Path,
    agent_name: str,
    *,
    reason: str,
    now: datetime | None = None,
) -> bool:
    if reason not in {"purged", "retention_expired"}:
        raise ValueError("invalid purge reason")
    state_path, token_path, home_path, mcp_path, lock_path = _paths(
        runtime_dir, agent_name
    )
    with _AgentLock(lock_path, exclusive=True):
        if not any(
            path.exists() or path.is_symlink()
            for path in (state_path, token_path, home_path, mcp_path)
        ):
            return False
        state: dict[str, Any] | None
        try:
            state = _load_state(state_path)
            _validate_identity(state, agent_name=agent_name)
        except ResumeStateError:
            state = None
        if (
            state is None
            or state.get("schema_version") != SCHEMA_VERSION
            or state.get("launch_origin") != "child"
            or state.get("provider") != "codex"
            or state.get("resume_in_progress_at") is not None
        ):
            return False
        if reason == "retention_expired":
            try:
                expires = _parse_timestamp(
                    state.get("resume_expires_at"), "resume_expires_at"
                )
            except ResumeStateError:
                return False
            if _utc_now(now) < expires:
                return False
        if state is not None:
            tombstone = {
                "schema_version": SCHEMA_VERSION,
                "reason": reason,
                "purged_at": _timestamp(_utc_now()),
                "provider": "codex",
                "agent_id": state["agent_id"],
                "agent_name": agent_name,
                "project_key": state["project_key"],
            }
            _atomic_json(
                runtime_dir
                / "child-resume-tombstones"
                / f"{state['agent_id']}.json",
                tombstone,
            )
        for path in (home_path, mcp_path, token_path, state_path):
            _remove_exact(path)
        return True


def discard_generated(runtime_dir: Path, agent_name: str) -> None:
    """Remove only rebuildable child config/runtime, preserving credentials."""

    _state, _token, home_path, mcp_path, lock_path = _paths(
        runtime_dir, agent_name
    )
    with _AgentLock(lock_path, exclusive=True):
        _remove_exact(home_path)
        _remove_exact(mcp_path)


def purge_expired(runtime_dir: Path, *, now: datetime | None = None) -> list[str]:
    removed: list[str] = []
    state_dir = runtime_dir / "child-agents"
    try:
        candidates = tuple(state_dir.glob("*.json"))
    except OSError:
        return removed
    current = _utc_now(now)
    for path in candidates:
        name = path.stem
        if not SAFE_NAME.fullmatch(name):
            continue
        try:
            state = _load_state(path)
            if state.get("launch_origin") != "child":
                continue
            if state.get("resume_in_progress_at") is not None:
                continue
            expires = _parse_timestamp(
                state.get("resume_expires_at"), "resume_expires_at"
            )
        except ResumeStateError:
            continue
        if current >= expires and purge_one(
            runtime_dir, name, reason="retention_expired", now=current
        ):
            removed.append(name)
    return removed


def _looks_like_agent_mail(name: str) -> bool:
    normalized = name.replace("-", "").replace("_", "").replace('"', "").lower()
    return normalized in {"agentmail", "mcpagentmail", "agentstackmail", "orrerymail"}


def _toml_string(value: str) -> str:
    encoded = json.dumps(value, ensure_ascii=False)
    return "".join(
        "\\u" + format(ord(char), "04x") if 0x7F <= ord(char) <= 0x9F else char
        for char in encoded
    )


def _toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_]+", value) else _toml_string(value)


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "-inf" if value < 0 else "inf"
        return repr(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(
            _toml_key(key) + " = " + _toml_value(item)
            for key, item in sorted(value.items())
        ) + " }"
    raise TypeError("unsupported TOML value: " + type(value).__name__)


def _emit_toml(config: dict[str, Any]) -> str:
    output: list[str] = []

    def emit_table(path: tuple[str, ...], table: dict[str, Any], kind: str | None):
        scalars = [
            (key, value)
            for key, value in table.items()
            if not isinstance(value, dict)
            and not (
                isinstance(value, list)
                and value
                and all(isinstance(item, dict) for item in value)
            )
        ]
        children = [(key, value) for key, value in table.items() if isinstance(value, dict)]
        arrays = [
            (key, value)
            for key, value in table.items()
            if isinstance(value, list)
            and value
            and all(isinstance(item, dict) for item in value)
        ]
        dotted = ".".join(_toml_key(part) for part in path)
        if kind == "table":
            output.append("[" + dotted + "]")
        elif kind == "array":
            output.append("[[" + dotted + "]]" )
        for key, value in sorted(scalars):
            output.append(_toml_key(key) + " = " + _toml_value(value))
        for key, value in sorted(children):
            if output and output[-1] != "":
                output.append("")
            emit_table(path + (key,), value, "table")
        for key, items in sorted(arrays):
            for item in items:
                if output and output[-1] != "":
                    output.append("")
                emit_table(path + (key,), item, "array")

    emit_table((), config, None)
    return "\n".join(output) + "\n"


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _drop_protected_overlay_tables(overlay: dict[str, Any]) -> None:
    servers = overlay.get("mcp_servers")
    if servers is not None and not isinstance(servers, dict):
        print(
            "[child-resume] warning: ignored protected Codex child config overlay key mcp_servers",
            file=os.sys.stderr,
        )
        del overlay["mcp_servers"]
    elif isinstance(servers, dict):
        for name in list(servers):
            if name == "agentstack" or _looks_like_agent_mail(name):
                print(
                    "[child-resume] warning: ignored protected Codex child config overlay key "
                    + ".".join(("mcp_servers", _toml_key(name))),
                    file=os.sys.stderr,
                )
                del servers[name]
    plugins = overlay.get("plugins")
    if plugins is not None and not isinstance(plugins, dict):
        del overlay["plugins"]
    elif isinstance(plugins, dict):
        for plugin_id, plugin in plugins.items():
            if not isinstance(plugin, dict):
                plugins[plugin_id] = {}
                continue
            plugin_servers = plugin.get("mcp_servers")
            if plugin_servers is not None and not isinstance(plugin_servers, dict):
                del plugin["mcp_servers"]
            elif isinstance(plugin_servers, dict):
                if "agentstack" in plugin_servers:
                    print(
                        "[child-resume] warning: ignored protected Codex child config overlay key "
                        + ".".join(
                            (
                                "plugins",
                                _toml_key(plugin_id),
                                "mcp_servers",
                                "agentstack",
                            )
                        ),
                        file=os.sys.stderr,
                    )
                    del plugin_servers["agentstack"]


def _plugin_name(header: str) -> str:
    match = re.match(r'^plugins\.(?:"([^"]+)"|([A-Za-z0-9_-]+))', header)
    return (match.group(1) or match.group(2)) if match else ""


def _build_home_unlocked(
    *,
    home: Path,
    source: Path,
    runner: Path,
    child: str,
    project_key: str,
    token_file: Path,
    mcp_url: str,
    mail_env: str,
    runtime_dir: Path,
    bearer_mode: str,
    python_bin: str,
    mcp_profile: str,
    overlay_setting: str = "",
) -> Path:
    if not SAFE_NAME.fullmatch(child) or mcp_profile not in MCP_PROFILES:
        raise ValueError("invalid child identity or MCP profile")
    if not source.is_dir() or not runner.is_file() or not os.access(runner, os.X_OK):
        raise ValueError("current Codex home or MCP proxy is unavailable")
    _read_private(token_file, "canonical Codex child credential", MAX_TOKEN_BYTES)
    if home.resolve() == source.resolve():
        raise ValueError("generated and source Codex homes must differ")

    home.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home.parent, 0o700)
    temporary = Path(tempfile.mkdtemp(prefix=f".{child}.codex-home.", dir=home.parent))
    try:
        os.chmod(temporary, 0o700)
        sandbox_metadata = {".git", ".agents", ".codex"}
        for entry in source.iterdir():
            if entry.name == "config.toml" or entry.name in sandbox_metadata:
                continue
            os.symlink(entry, temporary / entry.name)

        lines: list[str] = []
        skipping = False
        claimed: list[str] = []
        plugin_ids: list[str] = []
        config_source = source / "config.toml"
        text = config_source.read_text(encoding="utf-8") if config_source.exists() else ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                header = stripped.strip("[]").strip()
                server = ""
                if header.startswith("mcp_servers."):
                    server = header[len("mcp_servers.") :].split(".")[0]
                name = server.strip('"')
                plugin_id = _plugin_name(header)
                if plugin_id.startswith("agentstack-codex-app@"):
                    if plugin_id not in plugin_ids:
                        plugin_ids.append(plugin_id)
                    plugin_prefix = "plugins." + _toml_string(plugin_id)
                    skipping = header.startswith(plugin_prefix + ".mcp_servers.agentstack")
                else:
                    skipping = False
                if name and (_looks_like_agent_mail(name) or name == "agentstack"):
                    skipping = True
                if skipping and name and name not in claimed:
                    claimed.append(name)
            if not skipping:
                lines.append(line)
        if not claimed:
            claimed = ["orrery-mail"]
        elif "orrery-mail" not in claimed:
            claimed.append("orrery-mail")
        if "agentstack" not in claimed:
            claimed.append("agentstack")

        lines.extend(
            [
                "",
                "# Written by spawn_child.sh: this child talks to ORRERY Mail through the",
                "# local proxy, which authenticates every call with the child's own token.",
            ]
        )
        for plugin_id in plugin_ids:
            lines.extend(
                [
                    "",
                    "[plugins." + _toml_string(plugin_id) + ".mcp_servers.agentstack]",
                    "enabled = false",
                ]
            )
        proxy_tools = (
            "bootstrap",
            "fetch_inbox",
            "send_message",
            "acknowledge_message",
            "reserve_files",
            "renew_reservations",
            "release_reservations",
            "runtime_status",
            "whois",
        )
        for name in claimed:
            key = "mcp_servers." + _toml_string(name)
            lines.extend(
                [
                    "",
                    "[" + key + "]",
                    "command = " + _toml_string(os.fspath(runner)),
                    "args = []",
                    "",
                    "[" + key + ".env]",
                    "AGENTSTACK_PROXY_AGENT_NAME = " + _toml_string(child),
                    "AGENTSTACK_PROXY_TOKEN_FILE = " + _toml_string(os.fspath(token_file)),
                    "AGENTSTACK_PROXY_PROGRAM = " + _toml_string("codex"),
                    "AGENTSTACK_PROJECT_KEY = " + _toml_string(project_key),
                    "AGENTSTACK_MCP_URL = " + _toml_string(mcp_url),
                    "AGENTSTACK_MAIL_ENV = " + _toml_string(mail_env),
                    "AGENTSTACK_MAIL_HTTP_BEARER_MODE = " + _toml_string(bearer_mode),
                    "AGENTSTACK_RUNTIME_DIR = " + _toml_string(os.fspath(runtime_dir)),
                ]
            )
            if python_bin:
                lines.append("AGENTSTACK_PYTHON = " + _toml_string(python_bin))
            lines.append(
                "AGENTSTACK_CODEX_APP_RUNTIME_DIR = "
                + _toml_string(os.fspath(home / "proxy-runtime"))
            )
            for tool_name in proxy_tools:
                lines.extend(
                    [
                        "",
                        "[" + key + ".tools." + _toml_string(tool_name) + "]",
                        'approval_mode = "approve"',
                    ]
                )
        config_text = "\n".join(lines) + "\n"
        if overlay_setting.strip():
            try:
                overlay = tomllib.loads(
                    Path(overlay_setting).read_text(encoding="utf-8")
                )
                config = tomllib.loads(config_text)
                _drop_protected_overlay_tables(overlay)
                _deep_merge(config, overlay)
                candidate = _emit_toml(config)
                tomllib.loads(candidate)
                config_text = candidate
            except Exception as exc:
                print(
                    "[child-resume] warning: could not apply Codex child config overlay "
                    + _toml_string(overlay_setting)
                    + ": "
                    + str(exc)
                    + "; continuing without it",
                    file=os.sys.stderr,
                )
        if mcp_profile == "orrery-only":
            config = tomllib.loads(config_text)
            for name, server in config.get("mcp_servers", {}).items():
                if name == "agentstack" or _looks_like_agent_mail(name):
                    continue
                if isinstance(server, dict):
                    server["enabled"] = False
            for plugin_id, plugin in config.get("plugins", {}).items():
                if plugin_id.startswith("agentstack-codex-app@"):
                    continue
                if isinstance(plugin, dict):
                    plugin["enabled"] = False
            config_text = _emit_toml(config)
        target = temporary / "config.toml"
        target.write_text(config_text, encoding="utf-8")
        os.chmod(target, 0o600)

        _remove_exact(home)
        os.replace(temporary, home)
        return home
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def build_home(
    *,
    home: Path,
    source: Path,
    runner: Path,
    child: str,
    project_key: str,
    token_file: Path,
    mcp_url: str,
    mail_env: str,
    runtime_dir: Path,
    bearer_mode: str,
    python_bin: str,
    mcp_profile: str,
    overlay_setting: str = "",
) -> Path:
    """Build only the canonical generated home while holding the child lock."""

    _state, _token, expected_home, _mcp, lock_path = _paths(
        runtime_dir, child
    )
    if home.absolute() != expected_home.absolute():
        raise ValueError("generated Codex home is not canonical for this child")
    with _AgentLock(lock_path, exclusive=True):
        return _build_home_unlocked(
            home=home,
            source=source,
            runner=runner,
            child=child,
            project_key=project_key,
            token_file=token_file,
            mcp_url=mcp_url,
            mail_env=mail_env,
            runtime_dir=runtime_dir,
            bearer_mode=bearer_mode,
            python_bin=python_bin,
            mcp_profile=mcp_profile,
            overlay_setting=overlay_setting,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    active = sub.add_parser("prepare-active")
    active.add_argument("--runtime-dir", required=True)
    active.add_argument("--agent-name", required=True)
    active.add_argument("--project-key", required=True)
    active.add_argument("--mcp-profile", choices=sorted(MCP_PROFILES), required=True)
    retired = sub.add_parser("mark-retired")
    retired.add_argument("--runtime-dir", required=True)
    retired.add_argument("--agent-name", required=True)
    retired.add_argument("--retention-days", type=int, required=True)
    begin = sub.add_parser("begin-resume")
    begin.add_argument("--runtime-dir", required=True)
    begin.add_argument("--agent-name", required=True)
    begin.add_argument("--agent-id", type=int, required=True)
    begin.add_argument("--project-key", required=True)
    begin.add_argument("--program", required=True)
    begin.add_argument("--mcp-profile", choices=sorted(MCP_PROFILES), required=True)
    cancel = sub.add_parser("cancel-resume")
    cancel.add_argument("--runtime-dir", required=True)
    cancel.add_argument("--agent-name", required=True)
    cancel.add_argument("--agent-id", type=int, required=True)
    cancel.add_argument("--project-key", required=True)
    purge = sub.add_parser("purge")
    purge.add_argument("--runtime-dir", required=True)
    purge.add_argument("--expired", action="store_true")
    purge.add_argument("agent_name", nargs="?")
    discard = sub.add_parser("discard-generated")
    discard.add_argument("--runtime-dir", required=True)
    discard.add_argument("--agent-name", required=True)
    build = sub.add_parser("build-home")
    for name in (
        "runtime-dir",
        "home",
        "source",
        "runner",
        "child",
        "project-key",
        "token-file",
        "mcp-url",
        "mail-env",
        "bearer-mode",
        "mcp-profile",
    ):
        build.add_argument("--" + name, required=True)
    build.add_argument("--python-bin", default="")
    build.add_argument("--overlay", default="")
    args = parser.parse_args()
    try:
        runtime = Path(getattr(args, "runtime_dir", "")).expanduser()
        if args.command == "prepare-active":
            prepare_active_state(
                runtime,
                args.agent_name,
                project_key=args.project_key,
                mcp_profile=args.mcp_profile,
            )
        elif args.command == "mark-retired":
            print(
                "retained"
                if mark_retired(
                    runtime,
                    args.agent_name,
                    retention_days=args.retention_days,
                )
                else "delete"
            )
        elif args.command == "begin-resume":
            begin_resume(
                runtime,
                args.agent_name,
                agent_id=args.agent_id,
                project_key=args.project_key,
                program=args.program,
                mcp_profile=args.mcp_profile,
            )
        elif args.command == "cancel-resume":
            cancel_resume(
                runtime,
                args.agent_name,
                agent_id=args.agent_id,
                project_key=args.project_key,
            )
        elif args.command == "purge":
            if args.expired:
                if args.agent_name:
                    parser.error("agent_name cannot be used with --expired")
                for name in purge_expired(runtime):
                    print(name)
            else:
                if not args.agent_name:
                    parser.error("agent_name is required without --expired")
                if not purge_one(runtime, args.agent_name, reason="purged"):
                    raise ResumeStateError(
                        "credential_missing",
                        "no retained Codex child resume material matched this identity",
                    )
                print(args.agent_name)
        elif args.command == "discard-generated":
            discard_generated(runtime, args.agent_name)
        elif args.command == "build-home":
            home = build_home(
                home=Path(args.home).expanduser(),
                source=Path(args.source).expanduser(),
                runner=Path(args.runner).expanduser(),
                child=args.child,
                project_key=args.project_key,
                token_file=Path(args.token_file).expanduser(),
                mcp_url=args.mcp_url,
                mail_env=args.mail_env,
                runtime_dir=runtime,
                bearer_mode=args.bearer_mode,
                python_bin=args.python_bin,
                mcp_profile=args.mcp_profile,
                overlay_setting=args.overlay,
            )
            print(home)
    except (OSError, ValueError, ResumeStateError) as exc:
        print(f"child_resume: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
