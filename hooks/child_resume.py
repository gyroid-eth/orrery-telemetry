#!/usr/bin/env python3
"""Private Claude/Codex child resume material and regenerated-home lifecycle.

The retained state contains an owner credential and is therefore deliberately
kept outside the dashboard API.  This module is shared by fresh child launches,
normal cleanup, dashboard resume, explicit purge, and expiry maintenance.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
import secrets
import fcntl
import hmac
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import runpy
import sys
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable


SCHEMA_VERSION = 1
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
# The Mail proxy's agent-name rule (mcp_server._AGENT_NAME); it refuses to
# start with any other parent name.
PROXY_AGENT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,127}")
CODEX_PROGRAMS = {"codex", "codex-cli"}
CLAUDE_PROGRAMS = {"claude", "claude-code"}
MCP_PROFILES = {"inherit", "orrery-only"}
MAX_STATE_BYTES = 65536
MAX_TOKEN_BYTES = 4096


def _provider(program: object) -> str | None:
    if isinstance(program, str):
        if program in CODEX_PROGRAMS:
            return "codex"
        if program in CLAUDE_PROGRAMS:
            return "claude"
    return None


def _retained_shape(state: dict[str, Any]) -> bool:
    provider = _provider(state.get("program"))
    return (
        provider is not None
        and state.get("schema_version") == SCHEMA_VERSION
        and state.get("launch_origin") == "child"
        and state.get("provider") == provider
        and (
            provider != "codex"
            or (isinstance(state.get("codex_mcp_profile"), str)
                and state["codex_mcp_profile"] in MCP_PROFILES)
        )
    )


# What a person reads when a resume is refused for state a crash left behind.
NOT_ENDED_NORMALLY = (
    "This child did not end normally (for example, the computer was shut down or "
    "forced off), so it was never retired. The dashboard makes such a child "
    "resumable when it starts after a reboot; if it is still refused, a session of "
    "this child may still be running (look for its tmux session)."
)
RESUME_NOT_FINISHED = (
    "A resume of this child started and did not finish (for example, the computer "
    "went off right after). The dashboard clears this when it starts after a "
    "reboot; if it is still refused, the resumed session may still be starting."
)


def require_retired(state: dict[str, Any]) -> None:
    """Refuse a child state a crash left running, saying so in plain words."""
    if state.get("retired_at") is None and state.get("resume_in_progress_at") is None:
        raise ResumeStateError("config_unrestorable", NOT_ENDED_NORMALLY)


class ResumeStateError(ValueError):
    """A stable fail-closed reason suitable for dashboard capability output."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# A request owns only its generation. Other resume requests fail closed while
# the selection is staged; a separate lock serializes dashboard jumps.
_TOOLS_CHANGE = ContextVar("child_tools_change", default="")


def tools_change_generation() -> str:
    return _TOOLS_CHANGE.get() or os.environ.get("AGENTSTACK_TOOLS_CHANGE_GENERATION", "")


@contextmanager
def owned_tools_change(generation: str):
    token = _TOOLS_CHANGE.set(generation)
    try:
        yield
    finally:
        _TOOLS_CHANGE.reset(token)


def _tools_journal(state_path: Path) -> Path:
    return state_path.with_name(f".{state_path.stem}.tools-change.json")


def _check_tools_change(state: dict, state_path: Path) -> None:
    pending = state.get("tools_change_generation")
    journal = _tools_journal(state_path)
    if os.path.lexists(journal):
        saved = json.loads(_read_private(journal, "tools change journal", 16384))
        pending = saved.get("generation") or "invalid"
    if pending and pending != tools_change_generation():
        label = pending if isinstance(pending, str) and re.fullmatch(r"[a-f0-9]{32}", pending) else "invalid"
        raise ResumeStateError("config_unrestorable", f"A tools change ({label}) is still pending; finish or roll back that generation first")


def tools_change_lock(runtime: Path, name: str):
    state, *_ = _paths(runtime, name)
    return _AgentLock(state.with_name(f".{name}.tools-change.lock"), exclusive=True)


def _tools_record_is_canonical(runtime: Path, name: str, record: Path) -> bool:
    state_path, *_ = _paths(runtime, name)
    return record.parent == state_path.parent and (record == tools_record_path(runtime, name) or
        re.fullmatch(re.escape(name) + r"\.claude-launch\.[A-Za-z0-9-]{8,128}\.json", record.name) is not None)


def _file_identity(path: Path) -> list[int] | None:
    try:
        info = path.lstat()
        return [info.st_dev, info.st_ino]
    except FileNotFoundError:
        return None


def begin_tools_change(runtime: Path, name: str, validated: dict,
                       record: Path, candidate: dict, profile: str | None) -> str:
    """Write intent before staging; recovery never reads generated config."""
    state_path, _, home, mcp, lock = _paths(runtime, name)
    if not _tools_record_is_canonical(runtime, name, record):
        raise ResumeStateError("invalid_identity", "Tools record is not canonical")
    raw_candidate = (json.dumps(candidate, separators=(",", ":"), sort_keys=True) + "\n").encode()
    if len(raw_candidate) > 4096:
        raise ResumeStateError("config_unrestorable", "The tools selection is too large")
    generation = secrets.token_hex(16)
    journal = _tools_journal(state_path)
    created = False
    try:
        with _AgentLock(lock, exclusive=True):
            state = _load_state(state_path)
            if state != validated or state.get("tools_change_generation") or os.path.lexists(journal):
                raise ResumeStateError("config_unrestorable", "Child state changed before the tools change")
            old = _read_private(record, "tools selection", 4096).hex() if os.path.lexists(record) else None
            saved = {"generation": generation,
                     "identity": {key: state.get(key) for key in ("agent_id", "agent_name", "project_key", "program")},
                     "record": record.name, "old_record": old,
                     "old_profile": state.get("codex_mcp_profile"),
                     "new_profile": profile if profile is not None else state.get("codex_mcp_profile"),
                     "candidate_sha256": hashlib.sha256(raw_candidate).hexdigest(),
                     "generated": {path.name: _file_identity(path) for path in (home, mcp)},
                     "phase": "prepared"}
            _atomic_json(journal, saved)
            created = True
            state["tools_change_generation"] = generation
            if profile is not None:
                state["codex_mcp_profile"] = profile
            _atomic_json(state_path, state)
            for path in (home, mcp):
                if saved["generated"][path.name] is not None:
                    os.replace(path, path.with_name(f".{path.name}.tools-{generation}"))
            _atomic_json(record, candidate)
            saved["phase"] = "ready"
            _atomic_json(journal, saved)
    except Exception:
        if created:
            finish_tools_change(runtime, name, generation, commit=False)
        raise
    return generation


def finish_tools_change(runtime: Path, name: str, generation: str, *, commit: bool) -> None:
    """Persist the decision, then perform idempotent recovery of that decision.

    A durable commit cannot later be undone by a rollback retry. A journal left
    before the state marker, or after its removal, still owns the same identity.
    """
    state_path, _, home, mcp, lock = _paths(runtime, name)
    if not re.fullmatch(r"[a-f0-9]{32}", generation):
        raise ResumeStateError("invalid_identity", "Invalid tools change generation")
    journal = _tools_journal(state_path)
    with _AgentLock(lock, exclusive=True):
        saved = json.loads(_read_private(journal, "tools change journal", 16384))
        state = _load_state(state_path)
        phase = saved.get("phase")
        if (saved.get("generation") != generation or phase not in {"prepared", "ready", "rollback", "commit"}
                or state.get("tools_change_generation") not in (None, generation)
                or saved.get("identity") != {key: state.get(key) for key in ("agent_id", "agent_name", "project_key", "program")}):
            raise ResumeStateError("identity_mismatch", "Tools change generation no longer owns this child")
        record = state_path.with_name(saved["record"])
        if not _tools_record_is_canonical(runtime, name, record):
            raise ResumeStateError("invalid_identity", "Invalid tools change record")
        raw = _read_private(record, "tools selection", 4096) if os.path.lexists(record) else None
        old = bytes.fromhex(saved["old_record"]) if saved["old_record"] is not None else None
        candidate_matches = raw is not None and hashlib.sha256(raw).hexdigest() == saved["candidate_sha256"]
        if not candidate_matches and not (phase in {"prepared", "rollback"} and raw == old):
            raise ResumeStateError("identity_mismatch", "The staged tools selection has changed")
        # Once recorded, the decision is authoritative on every retry.
        if phase not in {"rollback", "commit"}:
            if commit and phase != "ready":
                raise ResumeStateError("config_unrestorable", "Tools change was not fully staged")
            phase = "commit" if commit else "rollback"
            if phase == "rollback" and (state.get("retired_at") is None or state.get("resume_in_progress_at") is not None):
                raise ResumeStateError("config_unrestorable", "Cannot roll back a child whose resume is running")
            saved["phase"] = phase
            _atomic_json(journal, saved)
        if phase == "rollback":
            if state.get("retired_at") is None or state.get("resume_in_progress_at") is not None:
                raise ResumeStateError("config_unrestorable", "Cannot roll back a child whose resume is running")
            # Validate every original before touching any generated path. An
            # absent backup can mean either not yet moved or already restored.
            for path in (home, mcp):
                backup = path.with_name(f".{path.name}.tools-{generation}")
                original = saved["generated"][path.name]
                witness = backup if os.path.lexists(backup) else path
                if original is not None and _file_identity(witness) != original:
                    raise ResumeStateError("identity_mismatch", "Original generated settings no longer match")
            if old is None:
                record.unlink(missing_ok=True)
            elif raw != old:
                _atomic_bytes(record, old)
            for path in (home, mcp):
                backup = path.with_name(f".{path.name}.tools-{generation}")
                if os.path.lexists(backup):
                    _remove_exact(path)
                    os.replace(backup, path)
                elif saved["generated"][path.name] is None:
                    _remove_exact(path)
            if saved["old_profile"] is not None:
                state["codex_mcp_profile"] = saved["old_profile"]
        else:
            for path in (home, mcp):
                _remove_exact(path.with_name(f".{path.name}.tools-{generation}"))
        state.pop("tools_change_generation", None)
        _atomic_json(state_path, state)
        journal.unlink()

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
    _atomic_bytes(path, (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode())


def _atomic_bytes(path: Path, raw: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_private(path: Path, label: str, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
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
        value = json.loads(_read_private(path, "child state", MAX_STATE_BYTES))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResumeStateError(
            "config_unrestorable", "child state is invalid"
        ) from exc
    if not isinstance(value, dict):
        raise ResumeStateError("config_unrestorable", "child state is invalid")
    return value


def _paths(runtime_dir: Path, agent_name: str) -> tuple[Path, Path, Path, Path, Path]:
    if not SAFE_NAME.fullmatch(agent_name):
        raise ResumeStateError("invalid_identity", "child identity is unsafe")
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
        directory = None
        try:
            # Validate before creating anything; never repair existing permissions
            # or follow a replaced lock/directory into another identity's files.
            ancestor = self.path.parent
            while True:
                try:
                    info = ancestor.lstat()
                    break
                except FileNotFoundError:
                    ancestor = ancestor.parent
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o022):
                raise ResumeStateError("credential_permission", "child resume lock directory is unsafe")
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(directory)
            if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022):
                raise ResumeStateError("credential_permission", "child resume lock directory is unsafe")
            # Opening an existing private read-only lock also works with flock;
            # neither its contents nor its mode/mtime need to change.
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            try:
                self.descriptor = os.open(self.path.name, flags, dir_fd=directory)
            except FileNotFoundError:
                self.descriptor = os.open(
                    self.path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory
                )
            locked = os.fstat(self.descriptor)
            if (not stat.S_ISREG(locked.st_mode) or locked.st_uid != os.getuid()
                    or stat.S_IMODE(locked.st_mode) & 0o077 or locked.st_nlink != 1):
                raise ResumeStateError("credential_permission", "child resume lock is unsafe")
            fcntl.flock(
                self.descriptor, fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
            )
            current = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
            current_directory = self.path.parent.lstat()
            if ((current.st_dev, current.st_ino) != (locked.st_dev, locked.st_ino)
                    or current.st_uid != os.getuid() or stat.S_IMODE(current.st_mode) & 0o077
                    or current.st_nlink != 1
                    or (current_directory.st_dev, current_directory.st_ino) != (info.st_dev, info.st_ino)
                    or current_directory.st_uid != os.getuid()
                    or stat.S_IMODE(current_directory.st_mode) & 0o022):
                raise ResumeStateError("credential_permission", "child resume lock changed during acquisition")
            return self
        except (OSError, ResumeStateError) as exc:
            if self.descriptor is not None:
                os.close(self.descriptor)
                self.descriptor = None
            if isinstance(exc, ResumeStateError):
                raise
            raise ResumeStateError("credential_permission", "child resume lock is unsafe or unavailable") from exc
        finally:
            if directory is not None:
                os.close(directory)

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
        or _provider(actual_program) is None
        or (agent_id is not None and state["agent_id"] != agent_id)
        or (project_key is not None and state["project_key"] != project_key)
        or (
            program is not None
            and (_provider(program) is None or _provider(program) != _provider(actual_program))
        )
    ):
        raise ResumeStateError(
            "identity_mismatch", "child state belongs to another registration"
        )


def prepare_active_state(
    runtime_dir: Path, agent_name: str, *, project_key: str, mcp_profile: str = "inherit",
    program: str | None = None, generation: str | None = None, parent_agent: str | None = None,
) -> dict[str, Any]:
    """Mark the child active. ``parent_agent`` records the launcher's parent
    (``""`` for a standalone launch clears it; ``None`` leaves it as is) so a
    later resume can hand it to the Mail proxy again."""
    state_path, token_path, _home, _mcp, lock_path = _paths(runtime_dir, agent_name)
    if mcp_profile not in MCP_PROFILES:
        raise ResumeStateError("config_unrestorable", "invalid Codex MCP profile")
    if parent_agent and not PROXY_AGENT_NAME.fullmatch(parent_agent):
        raise ResumeStateError("invalid_identity", "The parent name cannot be given to the Mail proxy")
    with _AgentLock(lock_path, exclusive=True):
        if _legacy_pending(state_path).exists() or _legacy_pending(state_path).is_symlink():
            raise ResumeStateError("config_unrestorable", "Legacy child migration is still pending")
        pending = state_path.with_name(f".{agent_name}.registration-pending.json")
        if generation is not None or pending.exists() or pending.is_symlink():
            if not _registration_matches(pending, generation):
                raise ResumeStateError("identity_mismatch", "Child registration attempt has changed")
        state = _load_state(state_path)
        _check_tools_change(state, state_path)
        _validate_identity(state, agent_name=agent_name, project_key=project_key, program=program)
        state_token = state.get("registration_token")
        try:
            canonical = _read_private(
                token_path, "canonical child credential", MAX_TOKEN_BYTES
            ).decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ResumeStateError(
                "credential_missing", "canonical child credential is invalid"
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
                "child state and credential are from different registrations",
            )
        state.update(
            schema_version=SCHEMA_VERSION,
            launch_origin="child",
            provider=_provider(state["program"]),
            retired_at=None,
            resume_expires_at=None,
        )
        if state["provider"] == "codex":
            state["codex_mcp_profile"] = mcp_profile
        else:
            state.pop("codex_mcp_profile", None)
        state.pop("resume_in_progress_at", None)
        if parent_agent:
            state["parent_agent"] = parent_agent
        elif parent_agent is not None:
            state.pop("parent_agent", None)
        _atomic_json(state_path, state)
        tombstone = runtime_dir / "child-resume-tombstones" / f"{state['agent_id']}.json"
        tombstone.unlink(missing_ok=True)
        return state



def _legacy_pending(state_path: Path) -> Path:
    return state_path.with_name(f".{state_path.stem}.legacy-migration.json")


def inspect_legacy_claude(
    runtime_dir: Path, agent_name: str, *, agent_id: int, project_key: str, program: str,
) -> dict[str, Any] | None:
    """Recognize only the old three-field Claude state, without writing a lock/file."""
    state_path, token_path, _home, mcp_path, _lock = _paths(runtime_dir, agent_name)
    raw = _read_private(state_path, "child state", MAX_STATE_BYTES)
    try:
        state = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResumeStateError("config_unrestorable", "child state is invalid") from exc
    if not isinstance(state, dict) or set(state) != {"agent_name", "project_key", "registration_token"}:
        return None
    if (type(agent_id) is not int or agent_id <= 0 or _provider(program) != "claude"
            or state["agent_name"] != agent_name or state["project_key"] != project_key):
        raise ResumeStateError("identity_mismatch", "Legacy child identity does not match the registration")
    tombstone = runtime_dir / "child-resume-tombstones" / f"{agent_id}.json"
    if tombstone.exists() or tombstone.is_symlink():
        raise ResumeStateError("credential_missing", "Legacy child resume material was purged")
    pending = state_path.with_name(f".{agent_name}.registration-pending.json")
    if any(path.exists() or path.is_symlink() for path in (pending, _legacy_pending(state_path))):
        raise ResumeStateError("config_unrestorable", "Child registration or migration is still pending")
    token = state["registration_token"]
    try:
        canonical = _read_private(token_path, "canonical child credential", MAX_TOKEN_BYTES).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ResumeStateError("credential_missing", "canonical child credential is invalid") from exc
    if not isinstance(token, str) or not token or len(token.encode()) > MAX_TOKEN_BYTES or not canonical:
        raise ResumeStateError("credential_missing", "Legacy child credential is unavailable")
    if not hmac.compare_digest(token.encode(), canonical.encode()):
        raise ResumeStateError("identity_mismatch", "Legacy child credential and state disagree")
    if mcp_path.exists() or mcp_path.is_symlink():
        _read_private(mcp_path, "legacy child MCP config", MAX_STATE_BYTES)
    return {**state, "_legacy_sha256": hashlib.sha256(raw).hexdigest()}


def begin_legacy_claude_migration(
    runtime_dir: Path, agent_name: str, *, registration: dict[str, Any],
    legacy: dict[str, Any], generation: str, retention_days: int, now: datetime | None = None,
) -> dict[str, Any]:
    """After remote owner authentication, snapshot and claim this legacy resume."""
    state_path, token_path, _home, mcp_path, lock_path = _paths(runtime_dir, agent_name)
    _validate_identity(registration, agent_name=agent_name, program="claude-code")
    if type(retention_days) is not int or retention_days <= 0:
        raise ResumeStateError("retention_expired", "Legacy child retention is disabled")
    if not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise ResumeStateError("invalid_identity", "Invalid legacy migration generation")
    current = _utc_now(now)
    expires = current + timedelta(days=retention_days)
    with _AgentLock(lock_path, exclusive=True):
        checked = inspect_legacy_claude(runtime_dir, agent_name, agent_id=registration["agent_id"],
                                      project_key=registration["project_key"], program=registration["program"])
        if checked is None or checked != legacy:
            raise ResumeStateError("identity_mismatch", "Legacy child material changed during authentication")
        originals = {}
        for label, path in (("state", state_path), ("mcp", mcp_path)):
            originals[label] = None
            if path.exists() or path.is_symlink():
                raw = _read_private(path, label, MAX_STATE_BYTES)
                info = path.stat()
                originals[label] = {"raw": raw.hex(), "mode": stat.S_IMODE(info.st_mode),
                                    "mtime_ns": info.st_mtime_ns}
        identity = {key: registration[key] for key in ("agent_id", "agent_name", "project_key", "program", "project_id")}
        snapshot = {"generation": generation, "registration": identity,
                    "registration_token": checked["registration_token"], "originals": originals}
        pending = _legacy_pending(state_path)
        state = {**identity, "registration_token": checked["registration_token"],
                 "schema_version": SCHEMA_VERSION, "launch_origin": "child", "provider": "claude",
                 "retired_at": _timestamp(current), "resume_expires_at": _timestamp(expires),
                 "resume_in_progress_at": _timestamp(current), "legacy_migration_generation": generation}
        if len(json.dumps(state).encode()) > MAX_STATE_BYTES:
            raise ResumeStateError("config_unrestorable", "Migrated child state is too large")
        _atomic_json(pending, snapshot)
        try:
            _atomic_json(state_path, state)
        except Exception:
            _finish_legacy_claude_unlocked(runtime_dir, agent_name, generation=generation, rollback=True)
            raise
        return state


def _finish_legacy_claude_unlocked(runtime_dir: Path, agent_name: str, *, generation: str, rollback: bool,
                                   before_finish: Callable[[], None] | None = None) -> bool:
    state_path, token_path, _home, mcp_path, _lock = _paths(runtime_dir, agent_name)
    pending = _legacy_pending(state_path)
    if not pending.exists() and not pending.is_symlink():
        return False
    snapshot = json.loads(_read_private(pending, "legacy migration undo record", MAX_STATE_BYTES * 6))
    if snapshot.get("generation") != generation:
        return False
    registration = snapshot["registration"]
    _validate_identity(registration, agent_name=agent_name, program="claude-code")
    tombstone = runtime_dir / "child-resume-tombstones" / f"{registration['agent_id']}.json"
    registration_pending = state_path.with_name(f".{agent_name}.registration-pending.json")
    if any(path.exists() or path.is_symlink() for path in (tombstone, registration_pending)):
        return False
    raw = _read_private(state_path, "child state", MAX_STATE_BYTES)
    state = json.loads(raw)
    original = snapshot["originals"]["state"]
    if state.get("legacy_migration_generation") != generation and raw.hex() != original["raw"]:
        return False
    canonical = _read_private(token_path, "canonical child credential", MAX_TOKEN_BYTES).decode("utf-8").strip()
    if not hmac.compare_digest(canonical.encode(), snapshot["registration_token"].encode()):
        return False
    # Keep the claim locked while compensating remote Mail, before allowing a retry.
    if before_finish is not None:
        before_finish()
    if rollback:
        for label, path in (("state", state_path), ("mcp", mcp_path)):
            saved = snapshot["originals"][label]
            if saved is None:
                _remove_exact(path)
            else:
                _atomic_bytes(path, bytes.fromhex(saved["raw"]), saved["mode"])
                os.utime(path, ns=(path.stat().st_atime_ns, saved["mtime_ns"]))
    else:
        if state.get("legacy_migration_generation") != generation:
            return False
        state.pop("legacy_migration_generation")
        _atomic_json(state_path, state)
    pending.unlink()
    return True


def finish_legacy_claude_migration(runtime_dir: Path, agent_name: str, *, generation: str, rollback: bool,
                                  before_finish: Callable[[], None] | None = None) -> bool:
    """Touch only this migration's material; a purge or newer generation wins."""
    *_unused, lock_path = _paths(runtime_dir, agent_name)
    with _AgentLock(lock_path, exclusive=True):
        return _finish_legacy_claude_unlocked(runtime_dir, agent_name, generation=generation, rollback=rollback,
                                              before_finish=before_finish)

def stage_registration(
    runtime_dir: Path, agent_name: str, *, project_key: str, program: str, generation: str,
    source: Path | None = None, binding: Path | None = None, legacy_agent_id: int | None = None,
) -> Path:
    """Validate a preregistration before changing files; retain a private undo record.

    ``legacy_agent_id`` names the owner ORRERY Mail confirmed for a Claude child
    that still has the three-field state of an earlier version. Only then is
    that state accepted, and it is rewritten with the confirmed identity; the
    undo record keeps the original bytes, so a failed start restores them.
    """
    state_path, token_path, _home, _mcp, lock_path = _paths(runtime_dir, agent_name)
    pending = state_path.with_name(f".{agent_name}.registration-pending.json")
    if not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise ResumeStateError("invalid_identity", "Invalid child registration generation")
    with _AgentLock(lock_path, exclusive=True):
        if any(path.exists() or path.is_symlink() for path in (pending, _legacy_pending(state_path))):
            raise ResumeStateError("config_unrestorable", "A child registration change is already pending")
        if _provider(program) is None or (source is not None and binding is None):
            raise ResumeStateError("identity_mismatch", "Formal child registration metadata is required")
        if legacy_agent_id is not None:
            if source is not None:
                raise ResumeStateError("identity_mismatch", "A legacy child is launched from its own state")
            legacy = inspect_legacy_claude(runtime_dir, agent_name, agent_id=legacy_agent_id,
                                           project_key=project_key, program=program)
            if legacy is None:
                raise ResumeStateError("identity_mismatch", "Child state is not the legacy Claude shape")
            state = {"agent_id": legacy_agent_id, "agent_name": agent_name, "project_key": project_key,
                     "program": program, "registration_token": legacy["registration_token"]}
        else:
            state = _load_state(binding if source is not None and binding is not None else state_path)
        _validate_identity(state, agent_name=agent_name, project_key=project_key, program=program)
        if source is not None:
            token = _read_private(source, "token handoff", MAX_TOKEN_BYTES).decode("utf-8").strip()
            state = {key: state[key] for key in ("agent_id", "agent_name", "project_key", "program")}
            state["registration_token"] = token
        else:
            token = state.get("registration_token")
            if token_path.exists() or token_path.is_symlink():
                canonical = _read_private(token_path, "canonical credential", MAX_TOKEN_BYTES).decode("utf-8").strip()
                if not isinstance(token, str) or not hmac.compare_digest(canonical.encode(), token.encode()):
                    raise ResumeStateError("identity_mismatch", "Child credential and state disagree")
        if not isinstance(token, str) or not token or len(token.encode()) > MAX_TOKEN_BYTES:
            raise ResumeStateError("credential_missing", "Child credential is invalid")
        tombstone = runtime_dir / "child-resume-tombstones" / f"{state['agent_id']}.json"
        originals = {}
        for label, path, limit in (("token", token_path, MAX_TOKEN_BYTES),
                                   ("state", state_path, MAX_STATE_BYTES),
                                   ("tombstone", tombstone, MAX_STATE_BYTES)):
            originals[label] = None
            if path.exists() or path.is_symlink():
                originals[label] = {"raw": _read_private(path, label, limit).hex(),
                                    "mode": stat.S_IMODE(path.stat().st_mode)}
        _atomic_json(pending, {"agent_id": state["agent_id"], "generation": generation, "originals": originals})
        try:
            _atomic_bytes(token_path, token.encode())
            _atomic_json(state_path, state)
        except Exception:
            _finish_registration_unlocked(runtime_dir, agent_name, generation=generation, rollback=True)
            raise
    return token_path


def _registration_matches(pending: Path, generation: str | None) -> bool:
    if generation is None or not re.fullmatch(r"[0-9a-f]{32}", generation):
        return False
    if not pending.exists() and not pending.is_symlink():
        return False
    snapshot = json.loads(_read_private(pending, "registration undo record", MAX_STATE_BYTES * 6))
    return snapshot.get("generation") == generation


def _finish_registration_unlocked(runtime_dir: Path, agent_name: str, *, generation: str, rollback: bool) -> bool:
    state_path, token_path, _home, _mcp, _lock = _paths(runtime_dir, agent_name)
    pending = state_path.with_name(f".{agent_name}.registration-pending.json")
    if not _registration_matches(pending, generation):
        return False
    snapshot = json.loads(_read_private(pending, "registration undo record", MAX_STATE_BYTES * 6))
    if type(snapshot.get("agent_id")) is not int or snapshot["agent_id"] <= 0:
        raise ResumeStateError("config_unrestorable", "Invalid registration undo record")
    if rollback:
        tombstone = runtime_dir / "child-resume-tombstones" / f"{snapshot['agent_id']}.json"
        for label, path in (("token", token_path), ("state", state_path), ("tombstone", tombstone)):
            original = snapshot["originals"][label]
            if original is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_bytes(path, bytes.fromhex(original["raw"]), original["mode"])
    pending.unlink()
    return True


def finish_registration(runtime_dir: Path, agent_name: str, *, generation: str, rollback: bool) -> bool:
    """Restore the pre-launch files on failure, or discard the undo record on success."""
    *_paths_unused, lock_path = _paths(runtime_dir, agent_name)
    with _AgentLock(lock_path, exclusive=True):
        return _finish_registration_unlocked(runtime_dir, agent_name, generation=generation, rollback=rollback)


def mark_retired(
    runtime_dir: Path,
    agent_name: str,
    *,
    retention_days: int,
    now: datetime | None = None,
) -> bool:
    """Retain modern children, or preserve known legacy material without migration."""

    if retention_days < 0:
        raise ValueError("retention days must be a non-negative integer")
    if retention_days == 0:
        return False
    state_path, token_path, _home, _mcp, lock_path = _paths(runtime_dir, agent_name)
    with _AgentLock(lock_path, exclusive=True):
        try:
            state = _load_state(state_path)
        except ResumeStateError:
            # Existing unsafe/unreadable material is not permission to delete
            # its credential. The cleanup caller already preserves on error.
            if state_path.exists() or state_path.is_symlink():
                raise
            return False
        if set(state) == {"agent_name", "project_key", "registration_token"}:
            if (state["agent_name"] != agent_name or not isinstance(state["project_key"], str)
                    or not state["project_key"].strip()):
                raise ResumeStateError("identity_mismatch", "Legacy cleanup identity is invalid")
            pending = state_path.with_name(f".{agent_name}.registration-pending.json")
            if any(path.exists() or path.is_symlink() for path in (pending, _legacy_pending(state_path))):
                raise ResumeStateError("config_unrestorable", "Legacy cleanup has a pending registration/migration")
            try:
                canonical = _read_private(token_path, "canonical child credential", MAX_TOKEN_BYTES).decode("utf-8").strip()
            except UnicodeDecodeError as exc:
                raise ResumeStateError("credential_missing", "canonical child credential is invalid") from exc
            token = state["registration_token"]
            if not isinstance(token, str) or not token or not canonical:
                raise ResumeStateError("credential_missing", "Legacy cleanup credential is unavailable")
            if not hmac.compare_digest(token.encode(), canonical.encode()):
                raise ResumeStateError("identity_mismatch", "Legacy cleanup credential and state disagree")
            # No ID/provider/timestamps are guessed here. Explicit resume
            # authenticates the formal owner before migrating these same bytes.
            return True
        try:
            _validate_identity(state, agent_name=agent_name)
        except ResumeStateError:
            return False
        if not _retained_shape(state):
            return False
        state_token = state.get("registration_token")
        try:
            canonical = _read_private(
                token_path, "canonical child credential", MAX_TOKEN_BYTES
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
    mcp_profile: str = "inherit",
    now: datetime | None = None,
) -> None:
    """Protect a validated retained credential from expiry while the resumed child runs."""

    state_path, token_path, _home, _mcp, lock_path = _paths(runtime_dir, agent_name)
    with _AgentLock(lock_path, exclusive=True):
        pending = state_path.with_name(f".{agent_name}.registration-pending.json")
        if any(path.exists() or path.is_symlink() for path in (pending, _legacy_pending(state_path))):
            raise ResumeStateError("config_unrestorable", "Child preregistration is still pending")
        state = _load_state(state_path)
        _check_tools_change(state, state_path)
        _validate_identity(
            state,
            agent_name=agent_name,
            agent_id=agent_id,
            project_key=project_key,
            program=program,
        )
        if (
            not _retained_shape(state)
            or (state.get("provider") == "codex" and (
                state.get("codex_mcp_profile") != mcp_profile
                or mcp_profile not in MCP_PROFILES
            ))
            or state.get("resume_in_progress_at") is not None
        ):
            raise ResumeStateError(
                "config_unrestorable", "child resume state is not ready"
            )
        _parse_timestamp(state.get("retired_at"), "retired_at")
        expires_at = _parse_timestamp(state.get("resume_expires_at"), "resume_expires_at")
        current = _utc_now(now)
        if current >= expires_at:
            raise ResumeStateError(
                "retention_expired", "child resume retention has expired"
            )
        state_token = state.get("registration_token")
        try:
            canonical = _read_private(
                token_path, "canonical child credential", MAX_TOKEN_BYTES
            ).decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ResumeStateError(
                "credential_missing", "canonical child credential is invalid"
            ) from exc
        if not isinstance(state_token, str) or not state_token or not canonical:
            raise ResumeStateError(
                "credential_missing", "retained child credential is unavailable"
            )
        if not hmac.compare_digest(
            canonical.encode("utf-8"), state_token.encode("utf-8")
        ):
            raise ResumeStateError(
                "identity_mismatch",
                "child state and credential are from different registrations",
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
    """Restore the retained state when bootstrap fails before CLI exec."""

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
            "credential_missing", "retained child state is unavailable"
        )
    with _AgentLock(lock_path, exclusive=False):
        pending = state_path.with_name(f".{agent_name}.registration-pending.json")
        if any(path.exists() or path.is_symlink() for path in (pending, _legacy_pending(state_path))):
            raise ResumeStateError("config_unrestorable", "Child preregistration is still pending")
        state = _load_state(state_path)
        _check_tools_change(state, state_path)
        _validate_identity(
            state,
            agent_name=agent_name,
            agent_id=agent_id,
            project_key=project_key,
            program=program,
        )
        if (
            not _retained_shape(state)
        ):
            raise ResumeStateError(
                "config_unrestorable", "child resume state is incomplete"
            )
        if state.get("resume_in_progress_at") is not None:
            raise ResumeStateError("config_unrestorable", RESUME_NOT_FINISHED)
        require_retired(state)
        _parse_timestamp(state.get("retired_at"), "retired_at")
        expires_at = _parse_timestamp(state.get("resume_expires_at"), "resume_expires_at")
        if _utc_now(now) >= expires_at:
            raise ResumeStateError(
                "retention_expired", "child resume retention has expired"
            )
        state_token = state.get("registration_token")
        try:
            canonical = _read_private(
                token_path, "canonical child credential", MAX_TOKEN_BYTES
            ).decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ResumeStateError(
                "credential_missing", "canonical child credential is invalid"
            ) from exc
        if not isinstance(state_token, str) or not state_token or not canonical:
            raise ResumeStateError(
                "credential_missing", "retained child credential is unavailable"
            )
        if not hmac.compare_digest(
            canonical.encode("utf-8"), state_token.encode("utf-8")
        ):
            raise ResumeStateError(
                "identity_mismatch",
                "child state and credential are from different registrations",
            )
        return state



def resume_eligibility(
    runtime_dir: Path,
    agent_name: str,
    *,
    agent_id: int,
    project_key: str,
    program: str,
    now: datetime | None = None,
) -> str:
    """Read-only: may a session reopened outside the dashboard unretire itself?

    The dashboard's resume unretires only retained material within its period
    or an owner token without child state. A terminal resume uses the same
    terms. Legacy three-field state is left for the dashboard's authenticated
    migration, and a tombstone keeps a purged or expired identity retired.
    """

    state_path, token_path, _home, _mcp, _lock = _paths(runtime_dir, agent_name)
    if state_path.exists() or state_path.is_symlink():
        state = _load_state(state_path)
        if set(state) == {"agent_name", "project_key", "registration_token"}:
            raise ResumeStateError("config_unrestorable", "legacy child state needs the dashboard's migration")
        inspect_retained(runtime_dir, agent_name, agent_id=agent_id, project_key=project_key,
                         program=program, now=now)
        return "retained"
    pending = state_path.with_name(f".{agent_name}.registration-pending.json")
    if any(path.exists() or path.is_symlink() for path in (pending, _legacy_pending(state_path))):
        raise ResumeStateError("config_unrestorable", "Child registration or migration is pending")
    try:
        inspect_retained(runtime_dir, agent_name, agent_id=agent_id, project_key=project_key, program=program, now=now)
    except ResumeStateError as exc:
        if exc.code != "credential_missing":
            raise
    try:
        token = _read_private(token_path, "owner credential", MAX_TOKEN_BYTES).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ResumeStateError("credential_missing", "owner credential is invalid") from exc
    if not token:
        raise ResumeStateError("credential_missing", "owner credential is empty")
    return "token_only"

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
            or state.get("provider") != _provider(state.get("program"))
            or state.get("resume_in_progress_at") is not None
            or state.get("tools_change_generation") is not None
            or os.path.lexists(_tools_journal(state_path))
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
                "provider": state["provider"],
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
        pending_path = state_path.with_name(f".{agent_name}.registration-pending.json")
        tools_path = tools_record_path(runtime_dir, agent_name)
        for path in (home_path, mcp_path, token_path, state_path, pending_path, _legacy_pending(state_path),
                     tools_path):
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


def _toml():
    """tomllib, imported where TOML is read: the lease and recovery commands
    must also run under an older python3 (3.9 on macOS), where it is absent."""
    import tomllib

    return tomllib


def boot_time() -> float | None:
    """When this machine last booted (epoch seconds), or None if unknown."""
    try:
        for line in Path("/proc/stat").read_text(encoding="ascii").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        import subprocess

        raw = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True,
                             text=True, timeout=5, check=False).stdout
        match = re.search(r"sec = (\d+)", raw)
        return float(match.group(1)) if match else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _recovery_candidate(state: dict[str, Any]) -> bool:
    if not _retained_shape(state):
        return False
    running = state.get("retired_at") is None and state.get("resume_in_progress_at") is None
    cut_off = state.get("retired_at") is not None and state.get("resume_in_progress_at") is not None
    return running or cut_off


def recover_after_reboot(
    runtime_dir: Path,
    *,
    is_live: Callable[[str], bool | None],
    retention_days: int,
    boot: float | None = None,
    now: datetime | None = None,
) -> list[tuple[str, str]]:
    """Make children that a reboot stopped mid-run resumable again.

    A child's state says "running" (retired_at null) until its normal exit
    retires it. A forced shutdown skips that exit, and every resume then
    refused with "retired_at is missing" (162nd seminar: after each reboot the
    agents could not resume until retired by hand). Likewise a resume that
    launched and was cut off before its session started stays "pending".

    Only state last written before this machine booted is touched: no process
    from before a boot can still be running, so that is proof, not a guess.
    It must also have no tmux session now (is_live: True = live, False = not
    live, None = cannot tell, which skips it), a matching owner credential,
    and no pending registration or migration (left as they are). Everything is
    re-read under the child's lock. Returns (name, "retired" | "resume_cleared").
    """
    recovered: list[tuple[str, str]] = []
    if retention_days <= 0:
        return recovered
    boot = boot_time() if boot is None else boot
    if boot is None:
        return recovered
    try:
        candidates = tuple((runtime_dir / "child-agents").glob("*.json"))
    except OSError:
        return recovered
    for path in candidates:
        name = path.stem
        if not SAFE_NAME.fullmatch(name):
            continue
        state_path, token_path, _home, _mcp, lock_path = _paths(runtime_dir, name)
        try:
            if state_path.lstat().st_mtime >= boot:
                continue
            # Only a child left running or mid-resume is a candidate. Ask nothing
            # about the rest (proxy configs, retired children, other files): the
            # liveness question runs tmux, once per candidate.
            if not _recovery_candidate(_load_state(state_path)):
                continue
            if is_live(name) is not False:
                continue
            with _AgentLock(lock_path, exclusive=True):
                if state_path.lstat().st_mtime >= boot:
                    continue
                pending = state_path.with_name(f".{name}.registration-pending.json")
                if any(item.exists() or item.is_symlink() for item in (pending, _legacy_pending(state_path))):
                    continue
                state = _load_state(state_path)
                _validate_identity(state, agent_name=name)
                if not _retained_shape(state):
                    continue
                canonical = _read_private(token_path, "canonical child credential",
                                          MAX_TOKEN_BYTES).decode("utf-8").strip()
                token = state.get("registration_token")
                if (not isinstance(token, str) or not token or not canonical
                        or not hmac.compare_digest(canonical.encode("utf-8"), token.encode("utf-8"))):
                    continue
                if state.get("retired_at") is None and state.get("resume_in_progress_at") is None:
                    retired = _utc_now(now)
                    state["retired_at"] = _timestamp(retired)
                    state["resume_expires_at"] = _timestamp(retired + timedelta(days=retention_days))
                    outcome = "retired"
                elif state.get("retired_at") is not None and state.get("resume_in_progress_at") is not None:
                    _parse_timestamp(state.get("retired_at"), "retired_at")
                    _parse_timestamp(state.get("resume_expires_at"), "resume_expires_at")
                    state.pop("resume_in_progress_at")
                    outcome = "resume_cleared"
                else:
                    continue
                # A session may have started while this waited for the child's
                # lock: look again (tmux and leases) before taking the lease lock.
                if is_live(name) is not False:
                    continue
                # Then only the leases again, and the write, under the lease lock
                # every lease writer takes: no lease can appear in between.
                with _LeaseLock(runtime_dir):
                    if live_lease(runtime_dir, name, boot=boot):
                        continue
                    _atomic_json(state_path, state)
                    os.chmod(token_path, 0o600)
                recovered.append((name, outcome))
        except (ResumeStateError, OSError, UnicodeDecodeError):
            continue
    return recovered


def _lease_alive(lease: Path, boot: float | None) -> bool:
    """A live-session lease names a running process only if it was written since
    this machine booted and that PID is alive. Before a boot, the PID may now
    belong to an unrelated process (a reboot reuses PIDs)."""
    if not lease.name.isdigit():
        return False
    try:
        if boot is not None and lease.stat().st_mtime < boot:
            return False
        os.kill(int(lease.name), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


class _LeaseLock:
    """One lock for every live-session lease: writing one (SessionStart), removing
    stale ones (prune_leases) and recovery's last look before it changes a
    child's state all hold it, so none of them acts on a lease the other is
    writing (#182 review). Nobody bypasses it. Holders keep it for milliseconds:
    inside it they only read and write lease files and child state (no tmux, no
    ps). A waiter that has waited `warn_after` seconds says so once on stderr and
    keeps waiting."""

    def __init__(self, runtime_dir: Path, *, warn_after: float = 10.0) -> None:
        self.root = runtime_dir / "live-sessions"
        self.warn_after = warn_after
        self.descriptor: int | None = None

    def __enter__(self) -> "_LeaseLock":
        import sys as _sys
        import time as _time

        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.descriptor = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        started, warned = _time.monotonic(), False
        while True:
            try:
                fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if not warned and _time.monotonic() - started >= self.warn_after:
                    print(f"child_resume: still waiting for {self.root / '.lock'}", file=_sys.stderr)
                    warned = True
                _time.sleep(0.02)

    def __exit__(self, *_exc: object) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


def write_lease(runtime_dir: Path, agent_name: str, pid: int) -> None:
    """Record that `pid` (a provider CLI) runs `agent_name`, under the lease lock."""
    if not SAFE_NAME.fullmatch(agent_name) or pid <= 1:
        raise ResumeStateError("invalid_identity", "lease name or PID is not valid")
    with _LeaseLock(runtime_dir) as lock:
        holder = lock.root / agent_name
        holder.mkdir(mode=0o700, exist_ok=True)
        (holder / str(pid)).write_bytes(b"")


def prune_leases(runtime_dir: Path, *, boot: float | None = None) -> None:
    """Drop leases whose process has ended or that predate the last boot."""
    boot = boot_time() if boot is None else boot
    root = runtime_dir / "live-sessions"
    if not root.is_dir():
        return
    with _LeaseLock(runtime_dir):
        try:
            holders = [path for path in root.iterdir() if path.is_dir()]
        except OSError:
            return
        for holder in holders:
            try:
                for lease in holder.iterdir():
                    if lease.name.isdigit() and not _lease_alive(lease, boot):
                        lease.unlink(missing_ok=True)
                holder.rmdir()
            except OSError:
                continue


def live_lease(runtime_dir: Path, agent_name: str, *, exclude_pid: int | None = None,
               boot: float | None = None) -> bool:
    """Is a session of this identity alive, by its leases (not counting exclude_pid)?"""
    boot = boot_time() if boot is None else boot
    holder = runtime_dir / "live-sessions" / agent_name
    try:
        leases = list(holder.iterdir()) if holder.is_dir() else []
    except OSError:
        return True  # cannot tell: say yes, as identity_live_elsewhere does
    return any(lease.name != str(exclude_pid) and _lease_alive(lease, boot) for lease in leases)


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


def _child_tools():
    """hooks/child_tools.py next to this file (the dashboard loads us by path)."""
    import importlib.util

    path = Path(__file__).resolve().parent / "child_tools.py"
    spec = importlib.util.spec_from_file_location("agentstack_child_tools", path)
    if spec is None or spec.loader is None:
        raise ValueError("child_tools.py is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tools_record_path(runtime_dir: Path, agent_name: str) -> Path:
    """The Codex child's base/tools selection, written by spawn_child.sh."""
    state_path = _paths(runtime_dir, agent_name)[0]
    return state_path.with_name(f"{agent_name}.tools.json")


def _plugin_name(header: str) -> str:
    match = re.match(r'^plugins\.(?:"([^"]+)"|([A-Za-z0-9_-]+))', header)
    return (match.group(1) or match.group(2)) if match else ""


def _git_dir_of(directory: Path) -> Path | None:
    """The git directory a checkout's `.git` names (a directory or a gitdir: file)."""
    marker = directory / ".git"
    try:
        if marker.is_dir():
            return marker.resolve()
        if marker.is_file():
            text = marker.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir:"):
                gitdir = Path(text[len("gitdir:"):].strip())
                return (gitdir if gitdir.is_absolute() else directory / gitdir).resolve()
    except (OSError, UnicodeError):
        return None
    return None


def _linked_worktree(work_dir: Path) -> tuple[Path, Path] | None:
    """(worktree root, shared git directory) when work_dir is in a linked worktree.

    Read from files only (the worktree's `.git` and its `commondir`), never by
    running git, so no GIT_* variable or repository config takes part. A main
    checkout and a submodule are not linked worktrees.
    """
    for directory in (work_dir, *work_dir.parents):
        marker = directory / ".git"
        if marker.is_dir():
            return None
        if not marker.is_file():
            continue
        gitdir = _git_dir_of(directory)
        if gitdir is None or gitdir.parent.name != "worktrees" or not gitdir.is_dir():
            return None
        # Only metadata git itself wrote for this very checkout counts: its
        # commondir names the shared directory, and its gitdir points back at
        # this `.git` (review of #174). A `.git` file merely naming some
        # repository's worktrees/ directory borrows nothing.
        try:
            pointer = (gitdir / "commondir").read_text(encoding="utf-8").strip()
            back = (gitdir / "gitdir").read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return None
        if not pointer or not back:
            return None
        back_path = Path(back) if Path(back).is_absolute() else gitdir / back
        try:
            if back_path.resolve() != marker.resolve():
                return None
            common = (gitdir / pointer).resolve()
        except OSError:
            return None
        # The metadata must live under worktrees/ of the very git directory
        # its commondir names: metadata kept in another repository cannot lend
        # itself a trusted repository's identity (re-check of #174).
        try:
            if not common.is_dir() or common != gitdir.parent.parent.resolve():
                return None
        except OSError:
            return None
        return directory, common
    return None


def _source_checkout(key_path: str, common: Path) -> Path | None:
    """The checkout above key_path whose `.git` names the shared git directory."""
    path = Path(key_path)
    if not path.is_absolute():
        return None
    for directory in path.parents:
        gitdir = _git_dir_of(directory)
        if gitdir is not None:
            return directory if gitdir == common else None
    return None


def _worktree_hook_trust(config_text: str, work_dir: Path | None) -> list[str]:
    """Config lines carrying a checkout's hook trust to its linked worktree.

    Codex keys hook trust by the hooks.json path, so the project hooks a user
    already trusted in a checkout wait for review again in its worktree, and do
    not run meanwhile (a Codex child stopped on "4 hooks need review" for 4.5
    minutes, 2026-10-01). The trust is a hash of the hook's content, which Codex
    checks again: copying it re-trusts only hooks that are identical to the ones
    the user trusted. The source checkout is the one whose `.git` names the same
    git directory as the worktree (also when it is kept elsewhere, as for the
    vault). Nothing the user has not trusted is added, an entry the user already
    has for the worktree path is kept, and only this child's config.toml is
    written.
    """
    if work_dir is None:
        return []
    found = _linked_worktree(work_dir)
    if found is None:
        return []
    worktree, common = found
    try:
        state = _toml().loads(config_text).get("hooks", {}).get("state", {})
    except _toml().TOMLDecodeError:
        return []
    if not isinstance(state, dict):
        return []
    roots = {os.fspath(worktree)}
    try:
        roots.add(os.fspath(worktree.resolve()))
    except OSError:
        pass
    # One checkout can be trusted under more than one path (its real path and
    # a symlink to it): every candidate for a worktree key is gathered first,
    # each key is written once, and a key whose candidates disagree is left to
    # Codex's own review rather than picked by order (review of #174).
    carried: dict[str, set[str]] = {}
    for key, entry in state.items():
        trusted = entry.get("trusted_hash") if isinstance(entry, dict) else None
        parts = key.rsplit(":", 3)
        if not isinstance(trusted, str) or len(parts) != 4:
            continue
        source = _source_checkout(parts[0], common)
        if source is None:
            continue
        relative = key[len(os.fspath(source)):]
        for root in sorted(roots):
            new_key = root + relative
            if new_key not in state:
                carried.setdefault(new_key, set()).add(trusted)
    lines: list[str] = []
    for new_key in sorted(carried):
        hashes = carried[new_key]
        if len(hashes) != 1:
            continue
        lines.extend(["", "[hooks.state." + _toml_string(new_key) + "]",
                      "trusted_hash = " + _toml_string(next(iter(hashes)))])
    if lines:
        lines[0:0] = ["", "# Written by spawn_child.sh: hook trust the user gave to this",
                      "# worktree's source checkout, for the same hooks here."]
    return lines


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
    parent_agent: str = "",
    work_dir: Path | None = None,
) -> Path:
    if not SAFE_NAME.fullmatch(child) or mcp_profile not in MCP_PROFILES:
        raise ValueError("invalid child identity or MCP profile")
    if parent_agent and not PROXY_AGENT_NAME.fullmatch(parent_agent):
        raise ValueError("the parent agent name cannot be given to the Mail proxy")
    # A selection is applied on every build (spawn and resume) or the build
    # fails: a child that asked for less must not silently get everything.
    # child_tools.py is loaded only for a child that has a record; a missing
    # helper then fails the build instead of skipping the selection.
    tools_spec = None
    record = tools_record_path(runtime_dir, child)
    if record.exists() or record.is_symlink():
        tools = _child_tools()
        try:
            tools_spec = tools.read_codex_record(os.fspath(record))
        except tools.ToolsError as exc:
            raise ValueError(str(exc)) from exc
    if tools_spec is not None and tools_spec["base"] == "mail-only" and mcp_profile != "orrery-only":
        raise ValueError("the tools record says mail-only but the MCP profile is " + mcp_profile)
    if not source.is_dir() or not runner.is_file() or not os.access(runner, os.X_OK):
        raise ValueError("current Codex home or MCP proxy is unavailable")
    _read_private(token_file, "canonical child credential", MAX_TOKEN_BYTES)
    if home.resolve() == source.resolve():
        raise ValueError("generated and source Codex homes must differ")

    # A user who only runs Codex through children may have no history yet.
    # Create shared storage before listing the source, otherwise Codex writes
    # real data into the disposable child home and cleanup removes it (#123).
    (source / "sessions").mkdir(mode=0o700, exist_ok=True)
    for name in ("history.jsonl", "session_index.jsonl"):
        try:
            fd = os.open(source / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # Existing records (including read-only files and symlinks) belong
            # to the user. Do not open them or change their metadata.
            continue
        else:
            os.close(fd)

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
            # The proxy reports this as lineage.parent_agent (standalone: none).
            if parent_agent:
                lines.append("AGENTSTACK_PROXY_PARENT_AGENT = " + _toml_string(parent_agent))
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
        lines.extend(_worktree_hook_trust("\n".join(lines) + "\n", work_dir))
        config_text = "\n".join(lines) + "\n"
        if overlay_setting.strip():
            try:
                overlay = _toml().loads(
                    Path(overlay_setting).read_text(encoding="utf-8")
                )
                config = _toml().loads(config_text)
                _drop_protected_overlay_tables(overlay)
                _deep_merge(config, overlay)
                candidate = _emit_toml(config)
                _toml().loads(candidate)
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
            config = _toml().loads(config_text)
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
        if tools_spec is not None:
            config = _toml().loads(config_text)
            try:
                tools.codex_apply(config, tools_spec)
            except tools.ToolsError as exc:
                raise ValueError(str(exc)) from exc
            config_text = _emit_toml(config)
            _toml().loads(config_text)
        target = temporary / "config.toml"
        target.write_text(config_text, encoding="utf-8")
        os.chmod(target, 0o600)

        _remove_exact(home)
        os.replace(temporary, home)
        return home
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _recorded_parent(state_path: Path) -> str:
    """The parent a launcher recorded for this child, or "" if none."""
    try:
        state = _load_state(state_path)
    except (ResumeStateError, OSError):
        return ""
    parent = state.get("parent_agent")
    if parent is None:
        return ""
    if not isinstance(parent, str) or not PROXY_AGENT_NAME.fullmatch(parent):
        raise ValueError("the recorded parent name cannot be given to the Mail proxy")
    return parent


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
    parent_agent: str = "",
    work_dir: Path | None = None,
) -> Path:
    """Build only the canonical generated home while holding the child lock."""

    _state, _token, expected_home, _mcp, lock_path = _paths(
        runtime_dir, child
    )
    if home.absolute() != expected_home.absolute():
        raise ValueError("generated Codex home is not canonical for this child")
    with _AgentLock(lock_path, exclusive=True):
        if _state.exists():
            _check_tools_change(_load_state(_state), _state)
        if not parent_agent:
            # A resume rebuilds the home without being told the parent; the
            # launcher recorded it in the child's state.
            parent_agent = _recorded_parent(_state)
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
            parent_agent=parent_agent,
            work_dir=work_dir,
        )


def main() -> int:
    helper=Path(__file__).resolve().parents[1]/'bin/lib/runtime_client.py'
    implicit=helper.parents[2]/'runtime-client.json'
    if os.environ.get('AGENTSTACK_CLIENT_CONFIG') or implicit.exists() or implicit.is_symlink():
        try:
            api=runpy.run_path(str(helper));client=api['configured']()
            if client is not None:
                with client.fence():pass
                raise api['ClientError']('GLOBAL_CHILD_USE_REGISTER_ENTRY')
        except (ValueError,RuntimeError,OSError) as exc:
            print('child-resume: '+str(exc),file=sys.stderr)
            return 2
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    recovery = sub.add_parser("rollback-tools-change")
    recovery.add_argument("--runtime-dir", required=True)
    recovery.add_argument("--agent-name", required=True)
    recovery.add_argument("--generation", required=True)
    active = sub.add_parser("prepare-active")
    active.add_argument("--runtime-dir", required=True)
    active.add_argument("--agent-name", required=True)
    active.add_argument("--project-key", required=True)
    active.add_argument("--mcp-profile", choices=sorted(MCP_PROFILES), default="inherit")
    active.add_argument("--program", choices=sorted(CODEX_PROGRAMS | CLAUDE_PROGRAMS))
    active.add_argument("--generation")
    active.add_argument("--parent-agent")
    stage = sub.add_parser("stage-registration")
    stage.add_argument("--runtime-dir", required=True)
    stage.add_argument("--agent-name", required=True)
    stage.add_argument("--project-key", required=True)
    stage.add_argument("--program", required=True, choices=sorted(CODEX_PROGRAMS | CLAUDE_PROGRAMS))
    stage.add_argument("--generation", required=True)
    stage.add_argument("--source")
    stage.add_argument("--binding")
    stage.add_argument("--legacy-agent-id", type=int)
    finish = sub.add_parser("finish-registration")
    finish.add_argument("--runtime-dir", required=True)
    finish.add_argument("--agent-name", required=True)
    finish.add_argument("--generation", required=True)
    finish.add_argument("--rollback", action="store_true")
    legacy_finish = sub.add_parser("finish-legacy-migration")
    legacy_finish.add_argument("--runtime-dir", required=True)
    legacy_finish.add_argument("--agent-name", required=True)
    legacy_finish.add_argument("--generation", required=True)
    legacy_finish.add_argument("--rollback", action="store_true")
    eligibility = sub.add_parser("resume-eligibility")
    eligibility.add_argument("--runtime-dir", required=True)
    eligibility.add_argument("--agent-name", required=True)
    eligibility.add_argument("--agent-id", type=int, required=True)
    eligibility.add_argument("--project-key", required=True)
    eligibility.add_argument("--program", required=True)
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
    begin.add_argument("--mcp-profile", choices=sorted(MCP_PROFILES), default="inherit")
    cancel = sub.add_parser("cancel-resume")
    cancel.add_argument("--runtime-dir", required=True)
    cancel.add_argument("--agent-name", required=True)
    cancel.add_argument("--agent-id", type=int, required=True)
    cancel.add_argument("--project-key", required=True)
    purge = sub.add_parser("purge")
    purge.add_argument("--runtime-dir", required=True)
    purge.add_argument("--expired", action="store_true")
    purge.add_argument("agent_name", nargs="?")
    prune = sub.add_parser("prune-leases")
    prune.add_argument("--runtime-dir", required=True)
    write = sub.add_parser("write-lease")
    write.add_argument("--runtime-dir", required=True)
    write.add_argument("--agent-name", required=True)
    write.add_argument("--pid", type=int, required=True)
    lease = sub.add_parser("live-lease")
    lease.add_argument("--runtime-dir", required=True)
    lease.add_argument("--agent-name", required=True)
    lease.add_argument("--exclude-pid", type=int, default=None)
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
    build.add_argument("--parent-agent", default="")
    build.add_argument("--work-dir", default="")
    args = parser.parse_args()
    try:
        runtime = Path(getattr(args, "runtime_dir", "")).expanduser()
        if args.command == "rollback-tools-change":
            with tools_change_lock(Path(args.runtime_dir), args.agent_name):
                finish_tools_change(Path(args.runtime_dir), args.agent_name, args.generation, commit=False)
        elif args.command == "prepare-active":
            prepare_active_state(
                runtime,
                args.agent_name,
                project_key=args.project_key,
                mcp_profile=args.mcp_profile,
                program=args.program, generation=args.generation,
                parent_agent=args.parent_agent,
            )
        elif args.command == "stage-registration":
            if bool(args.source) != bool(args.binding):
                parser.error("source and binding must be supplied together")
            print(stage_registration(runtime, args.agent_name, project_key=args.project_key,
                                     program=args.program, generation=args.generation, source=Path(args.source) if args.source else None,
                                     binding=Path(args.binding) if args.binding else None,
                                     legacy_agent_id=args.legacy_agent_id))
        elif args.command == "finish-registration":
            if not finish_registration(runtime, args.agent_name, generation=args.generation, rollback=args.rollback):
                print("child_resume: registration attempt is no longer pending; nothing changed", file=os.sys.stderr)
                return 3
        elif args.command == "finish-legacy-migration":
            if not finish_legacy_claude_migration(runtime, args.agent_name, generation=args.generation, rollback=args.rollback):
                print("child_resume: legacy migration is no longer pending; nothing changed", file=os.sys.stderr)
                return 3
        elif args.command == "resume-eligibility":
            print(resume_eligibility(runtime, args.agent_name, agent_id=args.agent_id,
                                     project_key=args.project_key, program=args.program))
        elif args.command == "prune-leases":
            prune_leases(runtime)
        elif args.command == "write-lease":
            write_lease(runtime, args.agent_name, args.pid)
        elif args.command == "live-lease":
            if not SAFE_NAME.fullmatch(args.agent_name):
                return 2
            return 0 if live_lease(runtime, args.agent_name, exclude_pid=args.exclude_pid) else 1
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
                        "no retained child resume material matched this identity",
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
                parent_agent=args.parent_agent,
                work_dir=Path(args.work_dir).expanduser() if args.work_dir else None,
            )
            print(home)
    except (OSError, ValueError, ResumeStateError) as exc:
        print(f"child_resume: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
