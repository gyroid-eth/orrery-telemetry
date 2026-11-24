"""Read bounded display metadata for the legacy agent roster."""

from functools import lru_cache
import json
import os
import re
import stat

try:
    from dashboard import claude_models, codex_models
except ModuleNotFoundError:  # direct dashboard entry point
    import claude_models
    import codex_models


HARNESS_KIND = {
    "claude": "claude", "claude-code": "claude",
    "codex": "codex", "codex-cli": "codex",
    "dsh": "dsh", "hermes": "hermes",
}
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}")
_PARENT = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,127}")
_CLAUDE_TAIL_CHUNK = 16 * 1024
_CLAUDE_TAIL_LIMIT = 256 * 1024


def absolute_cwd(value):
    if isinstance(value, str) and os.path.isabs(value) and value.isprintable():
        return value
    return None


def parent_name(value, name):
    if isinstance(value, str) and value != name and _PARENT.fullmatch(value):
        return value
    return None


def native_model_id(value):
    return value if isinstance(value, str) and _MODEL_ID.fullmatch(value) else None


def codex_model_id(value, catalog):
    if codex_models.is_model_id(value):
        return value
    if not value:
        return None
    label = value.lower().replace(" ", "-")
    matches = {model for model in catalog if model.lower().replace(" ", "-") == label}
    return next(iter(matches)) if len(matches) == 1 else None


def _file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def claude_turn(path, session_id):
    """Read the latest assistant turn of an already verified transcript."""
    if not path or not session_id:
        return None, None, None
    try:
        info = os.stat(path, follow_symlinks=False)
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
            return _claude_turn_cached(path, session_id, _file_identity(info))
    except OSError:
        pass
    return None, None, None


def _tail_lines(descriptor, size):
    lower = max(0, size - _CLAUDE_TAIL_LIMIT)
    end, partial = size, b""
    while end > lower:
        start = max(lower, end - _CLAUDE_TAIL_CHUNK)
        lines = (os.pread(descriptor, end - start, start) + partial).split(b"\n")
        partial = lines.pop(0) if start else b""
        yield from reversed(lines)
        end = start


@lru_cache(maxsize=4096)
def _claude_turn_cached(path, session_id, identity):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != identity:
            raise OSError("Claude transcript changed")
        turn = _last_claude_turn(_tail_lines(descriptor, opened.st_size), session_id)
        if _file_identity(os.fstat(descriptor)) != identity:
            raise OSError("Claude transcript changed")
        return turn
    finally:
        os.close(descriptor)


def _last_claude_turn(lines, session_id):
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if (not isinstance(record, dict) or record.get("type") != "assistant"
                or record.get("sessionId") != session_id or record.get("isSidechain") is True):
            continue
        message = record.get("message")
        model = message.get("model") if isinstance(message, dict) else None
        if model == "<synthetic>":
            continue
        effort = next((value for value in (record.get("perTurnEffort"), record.get("effort"))
                       if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value)), None)
        return (model if claude_models.is_model_id(model) else None, effort,
                absolute_cwd(record.get("cwd")))
    return None, None, None


def tool_permissions(spec, servers, *, chrome=False, isolated=False):
    """Project the configured capability from known launch evidence."""
    tools = (spec or {}).get("tools", {})
    screen = tools.get("screen", {})
    computer = {"read": "view", "operate": "control"}.get(screen.get("access"))
    cua = servers.get("cua-driver")
    if screen.get("server") != "cua-driver" and _cua_control(cua):
        computer = "control"
    browser = (chrome or "browser" in tools
               or _browser_enabled(servers.get("chrome-devtools")))
    if isolated and not tools.get("mcp"):
        if computer is None and (cua is None or isinstance(cua, dict) and cua.get("enabled") is False):
            computer = "off"
        if not browser and screen.get("server") in (None, "cua-driver", "chrome-devtools"):
            return computer, False
    return computer, True if browser else None


def _cua_control(server):
    # A relay can pass through the standard local command. Filters and
    # environment overrides still leave the mode unspecified.
    if not isinstance(server, dict):
        return False
    command, args, env = server.get("command"), server.get("args"), server.get("env", {})
    if not isinstance(command, str) or not command or not isinstance(args, list) or not args:
        return False
    if not all(isinstance(arg, str) for arg in args):
        return False
    argv = [command, *args]
    return (server.get("enabled") is not False
            and os.path.basename(argv[-2]) == "cua-driver" and argv[-1] == "mcp"
            and not any(key in server for key in ("enabled_tools", "disabled_tools", "tools", "omit_tools_from"))
            and isinstance(env, dict)
            and not any(key.startswith("CUA_DRIVER_") for key in env)
            and not any(key.startswith("CUA_DRIVER_") for key in os.environ))


def _browser_enabled(server):
    return (isinstance(server, dict) and server.get("enabled") is not False
            and bool(server.get("command") or server.get("url"))
            and "enabled_tools" not in server and not server.get("disabled_tools"))
