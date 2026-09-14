#!/usr/bin/env python3
"""Reserve and reconcile the ORRERY running-agent capacity.

The limit is deliberately host/runtime scoped.  It controls agent CLI
processes that this installation launches, not ORRERY Mail identities,
message history, or project ownership.  State lives below
``AGENTSTACK_RUNTIME_DIR`` so every launcher, including Dashboard, shares one
lock and one view of in-flight launches.

The script is a small command-line boundary instead of a long-lived daemon.
Each caller takes the lock, removes stale entries, checks capacity, and either
creates a short reservation or updates one after tmux has started the agent.
That makes concurrent launch requests safe without adding another resident
process to a memory-constrained host.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - native Windows rejects configured use
    fcntl = None  # type: ignore[assignment]


STATE_VERSION = 1
RESERVATION_SECONDS = 180
STARTING_SECONDS = 90
_LEASE_ID_RE = re.compile(r"[A-Za-z0-9_-]{12,128}\Z")
_SESSION_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_VERSION_COMMAND_RE = re.compile(r"\d+(?:\.\d+){1,3}\Z")


class CapacityError(RuntimeError):
    """An error that must prevent a configured limit from being bypassed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _limit_from_environment() -> int | None:
    raw = os.environ.get("AGENTSTACK_MAX_RUNNING_AGENTS", "").strip()
    if not raw:
        return None
    if re.fullmatch(r"[1-9][0-9]*", raw) is None:
        raise CapacityError(
            "invalid_running_agent_limit",
            "AGENTSTACK_MAX_RUNNING_AGENTS must be a positive integer",
        )
    return int(raw)


def _runtime_dir() -> Path:
    raw = os.environ.get("AGENTSTACK_RUNTIME_DIR", "").strip()
    path = Path(raw).expanduser() if raw else Path.home() / ".agentstack" / "runtime"
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError as exc:
        raise CapacityError(
            "running_agent_limit_unavailable",
            f"cannot create running-agent capacity directory {path}: {exc}",
        ) from exc
    return path


@contextlib.contextmanager
def _locked_runtime() -> Iterator[tuple[Path, Path]]:
    if fcntl is None:
        raise CapacityError(
            "running_agent_limit_unavailable",
            "running-agent capacity requires POSIX file locking",
        )
    runtime = _runtime_dir()
    lock_path = runtime / "running-agents.lock"
    try:
        with open(lock_path, "a+", encoding="utf-8") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield runtime, runtime / "running-agents.json"
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except CapacityError:
        raise
    except OSError as exc:
        raise CapacityError(
            "running_agent_limit_unavailable",
            f"cannot lock running-agent capacity state: {exc}",
        ) from exc


def _empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "leases": []}


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CapacityError(
            "running_agent_limit_state_invalid",
            f"cannot read running-agent capacity state {path}: {exc}",
        ) from exc
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        raise CapacityError(
            "running_agent_limit_state_invalid",
            f"running-agent capacity state {path} has an unsupported format",
        )
    leases = raw.get("leases")
    if not isinstance(leases, list):
        raise CapacityError(
            "running_agent_limit_state_invalid",
            f"running-agent capacity state {path} has no lease list",
        )
    for lease in leases:
        if not isinstance(lease, dict):
            raise CapacityError(
                "running_agent_limit_state_invalid",
                f"running-agent capacity state {path} contains an invalid lease",
            )
        lease_id = lease.get("id")
        if not isinstance(lease_id, str) or _LEASE_ID_RE.fullmatch(lease_id) is None:
            raise CapacityError(
                "running_agent_limit_state_invalid",
                f"running-agent capacity state {path} contains an invalid lease id",
            )
        if lease.get("state") not in {"reserved", "starting", "running"}:
            raise CapacityError(
                "running_agent_limit_state_invalid",
                f"running-agent capacity state {path} contains an invalid lease state",
            )
    return raw


def _write_state(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise CapacityError(
            "running_agent_limit_unavailable",
            f"cannot update running-agent capacity state {path}: {exc}",
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _process_tree() -> tuple[dict[int, str], dict[int, list[int]]]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,comm="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CapacityError(
            "running_agent_limit_unavailable",
            f"cannot inspect agent processes: {exc}",
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise CapacityError(
            "running_agent_limit_unavailable",
            "cannot inspect agent processes" + (f": {detail}" if detail else ""),
        )
    names: dict[int, str] = {}
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            pid = int(parts[0])
            parent = int(parts[1])
        except ValueError:
            continue
        names[pid] = os.path.basename(parts[2]).lower()
        children.setdefault(parent, []).append(pid)
    if not names:
        raise CapacityError(
            "running_agent_limit_unavailable",
            "agent process inspection returned no processes",
        )
    return names, children


def _program_matches(command: str, program: str | None) -> bool:
    command = os.path.basename(command or "").lower()
    if program and program.startswith("codex"):
        return command == "codex" or command.startswith("codex-")
    if program and program.startswith("claude"):
        return command in {"claude", "node"} or bool(_VERSION_COMMAND_RE.fullmatch(command))
    if program and program.startswith("antigravity"):
        return command == "agy"
    return (
        command in {"claude", "codex", "node", "agy"}
        or command.startswith("codex-")
        or bool(_VERSION_COMMAND_RE.fullmatch(command))
    )


def _agent_process_alive(
    pane_pid: Any,
    program: str | None,
    tree: tuple[dict[int, str], dict[int, list[int]]],
) -> bool:
    try:
        root = int(pane_pid)
    except (TypeError, ValueError):
        return False
    names, children = tree
    if root <= 0 or root not in names:
        return False
    pending = [root]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if _program_matches(names.get(pid, ""), program):
            return True
        pending.extend(children.get(pid, ()))
    return False


def _managed_agents_file(runtime: Path) -> Path:
    raw = os.environ.get("AGENTSTACK_MANAGED_AGENTS_FILE", "").strip()
    return Path(raw).expanduser() if raw else runtime / "managed_agents.txt"


def _read_managed_names(runtime: Path) -> set[str]:
    path = _managed_agents_file(runtime)
    try:
        if not path.exists():
            return set()
        return {
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if _SESSION_RE.fullmatch(line.strip() or "") is not None
        }
    except OSError as exc:
        raise CapacityError(
            "running_agent_limit_unavailable",
            f"cannot read managed agent list {path}: {exc}",
        ) from exc


def _managed_live_sessions(
    runtime: Path,
    tree: tuple[dict[int, str], dict[int, list[int]]],
    known_sessions: set[str],
) -> list[str]:
    names = _read_managed_names(runtime)
    if not names:
        return []
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CapacityError(
            "running_agent_limit_unavailable",
            f"cannot inspect tmux sessions: {exc}",
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().lower()
        # macOS tmux reports an absent server as "error connecting to ...
        # (no such file or directory)" rather than Linux's "no server
        # running".  It is still an empty tmux inventory, not an unavailable
        # process inspection.  Keep other tmux failures fail-closed.
        if (
            "no server running" in detail
            or "no sessions" in detail
            or ("error connecting to" in detail and "no such file or directory" in detail)
        ):
            return []
        raise CapacityError(
            "running_agent_limit_unavailable",
            "cannot inspect tmux sessions" + (f": {detail}" if detail else ""),
        )
    live: list[str] = []
    for line in result.stdout.splitlines():
        session, separator, pane_pid = line.partition("\t")
        if not separator or session not in names or session in known_sessions:
            continue
        if _agent_process_alive(pane_pid, None, tree):
            live.append(session)
    return sorted(set(live))


def _reconcile(
    runtime: Path, state: dict[str, Any], now: float
) -> tuple[dict[str, Any], list[str]]:
    """Drop dead leases and discover pre-existing managed agent processes.

    A process might have existed before the cap was enabled.  It is still
    counted through ``managed_agents.txt`` and tmux, so enabling the setting
    cannot silently make a live parent invisible.  The discovered sessions are
    intentionally not written to state: their lifecycle predates this feature,
    while new launchers get durable leases at admission time.
    """
    leases = state["leases"]
    need_tree = bool(leases) or bool(_read_managed_names(runtime))
    tree = _process_tree() if need_tree else None
    kept: list[dict[str, Any]] = []
    changed = False
    known_sessions: set[str] = set()
    for lease in leases:
        lease_state = lease["state"]
        session = lease.get("session")
        expires_at = lease.get("expires_at")
        try:
            expired = float(expires_at) <= now
        except (TypeError, ValueError):
            raise CapacityError(
                "running_agent_limit_state_invalid",
                "running-agent capacity state has an invalid expiration time",
            )
        if lease_state == "reserved":
            if expired:
                changed = True
                continue
            kept.append(lease)
            if isinstance(session, str) and session:
                known_sessions.add(session)
            continue
        if lease_state == "starting" and not expired:
            kept.append(lease)
            if isinstance(session, str) and session:
                known_sessions.add(session)
            continue
        if tree is None:
            raise CapacityError(
                "running_agent_limit_unavailable",
                "cannot verify a previously admitted running agent",
            )
        if _agent_process_alive(lease.get("pane_pid"), lease.get("program"), tree):
            if lease_state != "running":
                lease = dict(lease)
                lease["state"] = "running"
                lease["updated_at"] = now
                changed = True
            kept.append(lease)
            if isinstance(session, str) and session:
                known_sessions.add(session)
        else:
            changed = True
    if changed:
        state = dict(state)
        state["leases"] = kept
    external = _managed_live_sessions(runtime, tree, known_sessions) if tree else []
    return state, external


def _result_error(code: str, error: str, **fields: Any) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": error, **fields}


def reserve(purpose: str) -> dict[str, Any]:
    try:
        limit = _limit_from_environment()
        if limit is None:
            return {"ok": True, "limited": False, "lease_id": ""}
        with _locked_runtime() as (runtime, state_path):
            state, external = _reconcile(runtime, _read_state(state_path), time.time())
            now = time.time()
            leases = state["leases"]
            running = len(leases) + len(external)
            if running >= limit:
                _write_state(state_path, state)
                return _result_error(
                    "running_agent_limit_reached",
                    f"running-agent limit reached: {running} running, limit {limit}",
                    running=running,
                    limit=limit,
                    external_sessions=external,
                )
            lease_id = secrets.token_urlsafe(18)
            leases.append({
                "id": lease_id,
                "state": "reserved",
                "purpose": purpose[:120],
                "created_at": now,
                "updated_at": now,
                "expires_at": now + RESERVATION_SECONDS,
                "session": "",
                "program": "",
                "pane_pid": 0,
            })
            _write_state(state_path, state)
            return {
                "ok": True,
                "limited": True,
                "lease_id": lease_id,
                "running": running,
                "limit": limit,
            }
    except CapacityError as exc:
        return _result_error(exc.code, exc.message)


def claim(lease_id: str, session: str, pane_pid: str, program: str) -> dict[str, Any]:
    try:
        _limit_from_environment()
        if _LEASE_ID_RE.fullmatch(lease_id or "") is None:
            raise CapacityError("running_agent_limit_state_invalid", "running-agent lease id is invalid")
        if _SESSION_RE.fullmatch(session or "") is None:
            raise CapacityError("running_agent_limit_state_invalid", "running-agent session name is invalid")
        try:
            pid = int(pane_pid)
        except ValueError as exc:
            raise CapacityError(
                "running_agent_limit_unavailable",
                "cannot read tmux pane pid while claiming a running-agent slot",
            ) from exc
        if pid <= 0:
            raise CapacityError(
                "running_agent_limit_unavailable",
                "tmux returned no pane pid while claiming a running-agent slot",
            )
        with _locked_runtime() as (_runtime, state_path):
            state = _read_state(state_path)
            now = time.time()
            for lease in state["leases"]:
                if lease["id"] != lease_id:
                    continue
                if lease["state"] not in {"reserved", "starting"}:
                    raise CapacityError(
                        "running_agent_limit_state_invalid",
                        "running-agent lease was already claimed",
                    )
                lease.update({
                    "state": "starting",
                    "session": session,
                    "program": program,
                    "pane_pid": pid,
                    "updated_at": now,
                    "expires_at": now + STARTING_SECONDS,
                })
                _write_state(state_path, state)
                return {"ok": True, "lease_id": lease_id, "session": session}
            raise CapacityError(
                "running_agent_limit_state_invalid",
                "running-agent lease is missing or expired",
            )
    except CapacityError as exc:
        return _result_error(exc.code, exc.message)


def release(lease_id: str) -> dict[str, Any]:
    if not lease_id:
        return {"ok": True, "released": False}
    try:
        if _LEASE_ID_RE.fullmatch(lease_id) is None:
            raise CapacityError("running_agent_limit_state_invalid", "running-agent lease id is invalid")
        with _locked_runtime() as (_runtime, state_path):
            state = _read_state(state_path)
            retained = [lease for lease in state["leases"] if lease["id"] != lease_id]
            released = len(retained) != len(state["leases"])
            if released:
                state["leases"] = retained
                _write_state(state_path, state)
            return {"ok": True, "released": released}
    except CapacityError as exc:
        return _result_error(exc.code, exc.message)


def status() -> dict[str, Any]:
    try:
        limit = _limit_from_environment()
        if limit is None:
            return {"ok": True, "limited": False, "running": 0, "limit": None}
        with _locked_runtime() as (runtime, state_path):
            state, external = _reconcile(runtime, _read_state(state_path), time.time())
            _write_state(state_path, state)
            running = len(state["leases"]) + len(external)
            return {
                "ok": True,
                "limited": True,
                "running": running,
                "limit": limit,
                "available": max(limit - running, 0),
                "external_sessions": external,
            }
    except CapacityError as exc:
        return _result_error(exc.code, exc.message)


def _exit_code(result: dict[str, Any]) -> int:
    if result.get("ok"):
        return 0
    if result.get("code") == "running_agent_limit_reached":
        return 3
    if result.get("code") == "invalid_running_agent_limit":
        return 4
    return 5


def _emit(result: dict[str, Any], output: str) -> int:
    if output == "lease" and result.get("ok"):
        print(result.get("lease_id", ""))
    else:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    if not result.get("ok"):
        print(result.get("error", "running-agent capacity failed"), file=sys.stderr)
    return _exit_code(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", choices=("json", "lease"), default="json")
    subparsers = parser.add_subparsers(dest="command", required=True)
    reserve_parser = subparsers.add_parser("reserve")
    reserve_parser.add_argument("--purpose", default="launch")
    claim_parser = subparsers.add_parser("claim")
    claim_parser.add_argument("--lease", required=True)
    claim_parser.add_argument("--session", required=True)
    claim_parser.add_argument("--pane-pid", required=True)
    claim_parser.add_argument("--program", required=True)
    release_parser = subparsers.add_parser("release")
    release_parser.add_argument("--lease", required=True)
    subparsers.add_parser("status")
    args = parser.parse_args(argv)
    if args.command == "reserve":
        result = reserve(args.purpose)
    elif args.command == "claim":
        result = claim(args.lease, args.session, args.pane_pid, args.program)
    elif args.command == "release":
        result = release(args.lease)
    else:
        result = status()
    return _emit(result, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
