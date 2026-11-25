"""Read an agent's computer and browser use from its running process.

Used only when no saved launch settings describe the agent. The argv lists are
matched in memory and never returned; MCP environment values are not read.
"""

from functools import lru_cache
import ctypes
import ctypes.util
import json
import os
import re
import stat
import sys

try:
    from dashboard import agent_card
except ModuleNotFoundError:  # direct dashboard entry point
    import agent_card


_ARGV_LIMIT = 4 * 1024 * 1024
_CLAUDE_JSON_LIMIT = 64 * 1024 * 1024
_DEVTOOLS = re.compile(r"(?:^|/)chrome-devtools-mcp(?:@[^/\s]*)?(?:/|$)")


def _darwin_argv(pid):
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    argmax = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(argmax))
    if libc.sysctl((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(argmax), ctypes.byref(size), None, 0):
        return None  # CTL_KERN, KERN_ARGMAX
    size = ctypes.c_size_t(min(max(argmax.value, 4096), _ARGV_LIMIT))
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl((ctypes.c_int * 3)(1, 49, pid), 3, buffer, ctypes.byref(size), None, 0):
        return None  # CTL_KERN, KERN_PROCARGS2
    raw = buffer.raw[:size.value]
    if len(raw) < 4:
        return None
    count = int.from_bytes(raw[:4], sys.byteorder)
    # argc, the executable path, NUL padding, then argv.
    index = raw.find(b"\0", 4)
    if index < 0:
        return None
    while index < len(raw) and raw[index] == 0:
        index += 1
    values = []
    while len(values) < count:
        end = raw.find(b"\0", index)
        if end < 0:
            return None
        values.append(raw[index:end].decode("utf-8", "surrogateescape"))
        index = end + 1
    return values


def _linux_argv(pid):
    with open(f"/proc/{pid}/cmdline", "rb") as handle:
        raw = handle.read(_ARGV_LIMIT + 1)
    if not raw or len(raw) > _ARGV_LIMIT:
        return None
    return [item.decode("utf-8", "surrogateescape") for item in raw.rstrip(b"\0").split(b"\0")]


def argv(pid):
    """Return a process's exact argv, or None when it cannot be read."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        if sys.platform == "darwin":
            return _darwin_argv(pid)
        if sys.platform.startswith("linux"):
            return _linux_argv(pid)
    except (OSError, ValueError, AttributeError):
        pass
    return None


def is_cua(values):
    """A direct `cua-driver mcp` or a relay whose last two arguments start it."""
    return len(values) >= 2 and values[-1] == "mcp" and os.path.basename(values[-2]) == "cua-driver"


def is_screen(values):
    return any(values[index:index + 2] == ["-m", "agentstack_mail.screen_mcp"] for index in range(len(values)))


def is_devtools(values):
    # A shell's single command string never names the package on its own.
    return any(_DEVTOOLS.search(value) for value in values[1:] if not any(c.isspace() for c in value))


def chrome_flag(values):
    """The last explicit Claude in Chrome flag, or None when neither is given."""
    flag = None
    for value in values[1:]:
        if value in ("--chrome", "--no-chrome"):
            flag = value == "--chrome"
    return flag


@lru_cache(maxsize=4)
def _claude_settings(path, identity):
    with open(path, "rb") as handle:
        raw = handle.read(_CLAUDE_JSON_LIMIT + 1)
    if len(raw) > _CLAUDE_JSON_LIMIT:
        raise ValueError("Claude settings are too large")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Claude settings are not an object")

    def screen_names(servers):
        if not isinstance(servers, dict):
            return frozenset()
        return frozenset(name for name, server in servers.items()
                         if isinstance(name, str) and agent_card._cua_control(server))

    projects = {}
    for directory, project in (data.get("projects") or {}).items():
        if isinstance(directory, str) and isinstance(project, dict):
            disabled = project.get("disabledMcpServers")
            projects[directory] = (screen_names(project.get("mcpServers")),
                                   frozenset(disabled) if isinstance(disabled, list)
                                   and all(isinstance(name, str) for name in disabled) else frozenset())
    return data.get("claudeInChromeDefaultEnabled") is True, screen_names(data.get("mcpServers")), projects


def claude_settings(path):
    """(Chrome default, user Cua Driver names, per-project names) or None."""
    if not path:
        return None
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        return _claude_settings(path, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
    except (OSError, ValueError, RecursionError):
        return None


def configured_screen(settings, cwd):
    """The computer use a new Claude launch in `cwd` gets from the user's settings."""
    _chrome, user, projects = settings
    names, disabled = projects.get(cwd, (frozenset(), frozenset())) if cwd else (frozenset(), frozenset())
    return "control" if (user | names) - disabled else "off"


_RESUME_FLAGS = {"--chrome": 0, "--no-chrome": 0, "--mcp-config": 1, "--settings": 1, "--disallowed-tools": 1}


def dashboard_resume(values, name):
    """True when this is the command the dashboard's Resume starts, so it can start it again.

    Only the arguments the Resume itself adds may surround `--resume <id> -n <name>`;
    anything else (a model, channels, plugins) came from another launcher.
    """
    rest, kept, index = values[1:], [], 0
    while index < len(rest):
        width = _RESUME_FLAGS.get(rest[index])
        if width is None:
            kept.append(rest[index])
            index += 1
        else:
            index += 1 + width
    return len(kept) == 4 and kept[0] == "--resume" and kept[2] == "-n" and kept[3] == name


def observe(pid, process_tree, kind, *, cwd=None, claude_json=None, screen_mode=None, read=None, name=None):
    """Project the tools of a running Claude or Codex process.

    Returns {} when the process cannot be read. `*_next` is what the same
    launcher gives on its next start, when that can be known. With `name`, a
    Claude result also says whether the dashboard's Resume started it.
    """
    if kind not in ("claude", "codex") or process_tree is None:
        return {}
    read = read or argv
    own = read(pid)
    if not own:
        return {}
    _names, children = process_tree
    servers = [values for values in (read(child) for child in children.get(pid, ())) if values]
    screen = any(is_screen(values) for values in servers)
    computer = screen_mode if screen else "control" if any(is_cua(values) for values in servers) else "off"
    if kind == "codex":
        return {"computer_use": computer, "browser_use": any(is_devtools(values) for values in servers),
                "computer_use_next": None, "browser_use_next": None}
    settings = claude_settings(claude_json)
    flag = chrome_flag(own)
    browser = flag if flag is not None else settings[0] if settings else None
    if screen or "--strict-mcp-config" in own[1:]:
        computer_next = computer
    else:
        computer_next = configured_screen(settings, cwd) if settings else None
    result = {"computer_use": computer, "browser_use": browser,
              "computer_use_next": computer_next, "browser_use_next": browser}
    if name is not None:
        result["resume_launch"] = dashboard_resume(own, name)
    return result
