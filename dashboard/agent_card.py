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
    "antigravity": "antigravity",
}
AGY_LAUNCH_UNRECORDED = "Antigravity launch settings were not recorded"
AGY_BROWSER_UNRECORDED = "Antigravity browser mode was not recorded"
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
    if computer is None and "orrery-screen" in tools.get("mcp", ()):
        computer = "off"
    cua = servers.get("cua-driver")
    if screen.get("server") != "cua-driver" and _cua_control(cua):
        computer = "control"
    mode = tools.get("browser_mode")
    browser = None
    if tools.get("browser_off") is True:
        browser = False
    elif mode in ("off", "temporary", "personal"):
        browser = mode != "off"
    elif chrome or "browser" in tools or _browser_enabled(servers.get("chrome-devtools")):
        browser = True
    if isolated and not tools.get("mcp"):
        if computer is None and (cua is None or isinstance(cua, dict) and cua.get("enabled") is False):
            computer = "off"
        if browser is None and screen.get("server") in (None, "cua-driver", "chrome-devtools"):
            browser = False
    return computer, browser


def antigravity_tool_permissions(spec, computer=None, *, screen_live=False):
    if spec is None:
        return dict(permission_source="launch", computer_use=None, browser_use=None,
                    computer_use_reason=AGY_LAUNCH_UNRECORDED, browser_use_reason=AGY_LAUNCH_UNRECORDED)
    tools = spec["tools"]
    mode = tools.get("browser_mode")
    return dict(permission_source="launch", computer_use=computer,
                computer_use_source="relay" if screen_live else "launch",
                browser_use=None if mode is None else mode != "off",
                **({"browser_use_reason": AGY_BROWSER_UNRECORDED} if mode is None
                   else {"browser_use_source": "launch"}))


def permission_controls(row, browser_options=()):
    """Pair a display value with the action available for this roster row."""
    computer = row.get("computer_use")
    browser = row.get("browser_use")
    if (not row.get("harness_kind") and row.get("surface") != "codex-app"
            and row.get("lifecycle") != "persistent" and row.get("mail_status") != "unavailable"
            and computer is None and type(browser) is not bool):
        return None

    running = row.get("running")
    category = row.get("category")
    managed_launch = row.get("permission_managed_launch") is True
    standalone = (row.get("parent_state") == "top" or row.get("parentless") is True
                  or row.get("standalone") is True)
    reason = row.get("permission_resume_reason") if row.get("permission_resume_supported") is not True else None
    if row.get("permission_resume_supported") is not True:
        reason = reason or "Permission changes are unavailable"
    elif (not standalone and row.get("parent_state") != "child"
          or row.get("history_binding") != "bound" or not row.get("history_session_id")):
        reason = "No verified conversation" if standalone else "No verified child conversation"
    elif category == "finished" and not managed_launch:
        reason = "Exit this agent first"
    elif not (running is True and category == "agent"):
        if running is not False or (category not in {"gone", "retired"} and not (managed_launch and category == "finished")):
            reason = "Agent is not confirmed stopped"
        elif not managed_launch and row.get("resume_capability") not in {"ready", "verification_required"}:
            reason = "Conversation cannot be resumed"
    action = None if reason else "restart" if running is True else "resume"
    controls = {
        key: {
            "value": value,
            "source": row.get(f"{key}_source") or row.get("permission_source"),
            "observation_reason": row.get(f"{key}_reason"),
            "can_change": action is not None,
            "change_reason": reason,
            "action": action,
        }
        for key, value in (
            ("computer_use", computer if computer in ("off", "view", "control") else None),
            ("browser_use", browser if type(browser) is bool else None),
        )
    }
    browser_control = controls["browser_use"]
    mode = row.get("browser_mode") if browser is True else "off" if browser is False else None
    browser_reason = row.get("browser_change_reason")
    if action and not any(option != mode for option in browser_options):
        browser_reason = browser_reason or "No other Browser mode is available"
    if action and browser_reason:
        browser_control.update(can_change=False, change_reason=browser_reason, action=None)
    browser_control.update(mode=mode, options=list(browser_options) if browser_control["can_change"] else [])
    return controls


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
