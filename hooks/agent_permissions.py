"""Saved computer and browser use for agents that have no launch record.

A delegated child keeps its tools in its own launch record. A long-running or
resumed agent that was started some other way has none, so it followed the
user's settings and could not be changed from the UI. This module keeps one
small record per such agent and turns it into launch arguments, so the
dashboard Resume, `agentstack-persistent` and `agentstack-agent-permissions
exec` all apply the same record the same way.

Record: <runtime>/agent-permissions/<name>.json (0600). A missing field means
"follow the user's settings", which is the behavior without a record.
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
import fcntl
import json
import os
import re
import runpy
import secrets
import stat
import sys
import tempfile
import time


VERSION = 1
COMPUTER = ("off", "view", "control")
DIRECTORY = "agent-permissions"
SCREEN_SERVER = "orrery-screen"
LAUNCHERS = ("resume", "persistent", "exec")
MAX_BYTES = 16384
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_EXPLICIT_CHROME = ("--chrome", "--no-chrome")


class AgentPermissionsError(ValueError):
    """The saved permissions cannot be read, saved or applied as asked."""


@lru_cache(maxsize=1)
def _child_tools():
    return runpy.run_path(str(Path(__file__).with_name("child_tools.py")))


@lru_cache(maxsize=1)
def _screen_state():
    return runpy.run_path(str(Path(__file__).with_name("screen_state.py")))


def _name(name):
    if not isinstance(name, str) or not _NAME.fullmatch(name) or name.startswith("."):
        raise AgentPermissionsError("Invalid agent name")
    return name


def path(runtime, name) -> Path:
    return Path(runtime) / DIRECTORY / f"{_name(name)}.json"


def _validate(record, name):
    fields = {"version", "agent_name", "agent_id", "project_key", "program", "updated_at",
              "computer_use", "browser_use", "applied"}
    if (not isinstance(record, dict) or set(record) - fields or record.get("version") != VERSION
            or record.get("agent_name") != name or type(record.get("agent_id")) is not int
            or not isinstance(record.get("project_key"), str) or not record["project_key"]
            or not isinstance(record.get("program"), str) or not record["program"]
            or type(record.get("updated_at")) not in (int, float)
            or ("computer_use" in record and record["computer_use"] not in COMPUTER)
            or ("browser_use" in record and type(record["browser_use"]) is not bool)):
        raise AgentPermissionsError("Saved permissions are invalid")
    applied = record.get("applied")
    if applied is not None and (
            not isinstance(applied, dict) or set(applied) != {"launcher", "generation", "at"}
            or applied["launcher"] not in LAUNCHERS
            or not (applied["generation"] is None or isinstance(applied["generation"], str))
            or type(applied["at"]) not in (int, float)):
        raise AgentPermissionsError("Saved permissions are invalid")
    return record


def read(runtime, name, *, agent_id=None, project_key=None):
    """The agent's saved record, or None when there is none for this identity."""
    target = path(runtime, name)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AgentPermissionsError("Saved permissions cannot be read") from exc
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_size > MAX_BYTES):
        raise AgentPermissionsError("Saved permissions are not a private file")
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            record = json.loads(handle.read(MAX_BYTES + 1))
    except (OSError, ValueError, RecursionError) as exc:
        raise AgentPermissionsError("Saved permissions cannot be read") from exc
    record = _validate(record, name)
    # A record saved for an earlier registration of the same name is not this agent's.
    if ((agent_id is not None and record["agent_id"] != agent_id)
            or (project_key is not None and record["project_key"] != project_key)):
        return None
    return record


def _directory(runtime) -> Path:
    directory = Path(runtime) / DIRECTORY
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise AgentPermissionsError("The saved permissions directory is not private")
    return directory


@contextmanager
def _locked(runtime, name):
    directory = _directory(runtime)
    descriptor = os.open(directory / f".{_name(name)}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield directory
    finally:
        os.close(descriptor)


def _write(directory, name, record):
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, separators=(",", ":"), sort_keys=True)
        os.replace(temporary, directory / f"{name}.json")
    finally:
        Path(temporary).unlink(missing_ok=True)


def save(runtime, name, *, agent_id, project_key, program, **changes):
    """Set or clear (None) computer_use and browser_use; other fields are kept."""
    if not changes or set(changes) - {"computer_use", "browser_use"}:
        raise AgentPermissionsError("Choose computer_use or browser_use")
    if changes.get("computer_use") not in (None, *COMPUTER):
        raise AgentPermissionsError("computer_use must be off, view or control")
    if "browser_use" in changes and changes["browser_use"] is not None and type(changes["browser_use"]) is not bool:
        raise AgentPermissionsError("browser_use must be true or false")
    with _locked(runtime, name) as directory:
        try:
            current = read(runtime, name, agent_id=agent_id, project_key=project_key)
        except AgentPermissionsError:
            current = None  # An unreadable record is replaced by this explicit choice.
        record = dict(current or {})
        record.update(version=VERSION, agent_name=name, agent_id=agent_id, project_key=project_key,
                      program=program, updated_at=time.time())
        for key, value in changes.items():
            if value is None:
                record.pop(key, None)
            else:
                record[key] = value
        _write(directory, name, _validate(record, name))
        return record


def mark_applied(runtime, name, record, launcher, generation):
    """Note which launch applied the record; a newer save is left alone."""
    with _locked(runtime, name) as directory:
        current = read(runtime, name, agent_id=record["agent_id"], project_key=record["project_key"])
        if current is None or current["updated_at"] != record["updated_at"]:
            return
        current["applied"] = {"launcher": launcher, "generation": generation, "at": time.time()}
        _write(directory, name, _validate(current, name))


def _direct_screen_servers(claude_json, cwd):
    """Names of the user's own screen servers; the gated relay replaces them."""
    tools = _child_tools()
    try:
        definitions = tools["claude_mcp_definitions"](claude_json, cwd or "")
    except (OSError, ValueError):
        return []
    return sorted(name for name, definition in definitions.items()
                  if isinstance(name, str) and name != SCREEN_SERVER
                  and (tools["_cua_server"](name, definition)
                       or tools["_native_desktop_server"](name, definition if isinstance(definition, dict) else {})))


def claude_launch(runtime, record, command, *, cwd, claude_json, origin=None, mail_env=None, platform=None):
    """Arguments and MCP servers that apply `record` to a Claude launch `command`.

    Returns (arguments to append, servers to add, screen generation or None).
    A browser flag already in the command is the launcher's own choice and wins.
    """
    arguments, servers, generation = [], {}, None
    given = command[1:]
    if "browser_use" in record and not any(value in _EXPLICIT_CHROME for value in given):
        arguments.append("--chrome" if record["browser_use"] else "--no-chrome")
    if "computer_use" in record:
        if any(value == "--settings" or value.startswith("--settings=") for value in given):
            raise AgentPermissionsError("The launch command already passes --settings")
        if any(value == "--strict-mcp-config" for value in given):
            raise AgentPermissionsError("The launch command already restricts its MCP servers")
        generation = secrets.token_hex(16)
        state = _screen_state()
        try:
            servers[SCREEN_SERVER] = state["definition"](
                str(runtime), record["project_key"], record["agent_name"], generation,
                record["computer_use"], origin=origin, mail_env=mail_env)
        except (OSError, ValueError) as exc:
            raise AgentPermissionsError(f"Computer use could not be prepared: {exc}") from exc
        denied = _direct_screen_servers(claude_json, cwd)
        builtin = (platform or sys.platform) == "darwin"
        if builtin:
            denied = sorted({*denied, _child_tools()["BUILTIN_SCREEN"]})
        if denied:
            arguments += ["--settings", json.dumps(
                {"deniedMcpServers": [{"serverName": name} for name in denied]}, separators=(",", ":"))]
        if builtin:
            arguments += ["--disallowed-tools", "mcp__" + _child_tools()["BUILTIN_SCREEN"]]
    return arguments, servers, generation


def revoke(runtime, record, generation):
    """Give back a screen generation whose launch did not start."""
    if not generation:
        return
    state = _screen_state()
    store = state["Store"](str(runtime))
    store.revoke(dict(zip(state["TARGET"], (record["project_key"], state["launch_runtime"](str(runtime)),
                                              record["agent_name"], generation))))


def write_mcp_config(runtime, name, servers) -> Path:
    """The extra MCP servers as a private --mcp-config file next to the record."""
    with _locked(runtime, name) as directory:
        target = directory / f"{_name(name)}.mcp.json"
        descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".mcp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"mcpServers": servers}, handle, separators=(",", ":"))
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return target


def apply_to_claude(runtime, name, command, *, launcher, cwd, claude_json, agent_id=None, project_key=None,
                    origin=None, mail_env=None, platform=None):
    """Read, apply and mark one record. Returns the arguments to append ([] without a record)."""
    record = read(runtime, name, agent_id=agent_id, project_key=project_key)
    if record is None or not ({"computer_use", "browser_use"} & set(record)):
        return [], None, None
    arguments, servers, generation = claude_launch(
        runtime, record, command, cwd=cwd, claude_json=claude_json, origin=origin,
        mail_env=mail_env, platform=platform)
    try:
        if servers:
            arguments = ["--mcp-config", str(write_mcp_config(runtime, name, servers)), *arguments]
        mark_applied(runtime, name, record, launcher, generation)
    except BaseException:
        revoke(runtime, record, generation)
        raise
    return arguments, record, generation
