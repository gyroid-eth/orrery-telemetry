"""Read an agent's computer and browser use from its running process.

Used only when no saved launch settings describe the agent. The argv lists are
matched in memory and never returned; MCP environment values are not read.
"""

from functools import lru_cache
import ctypes
import ctypes.util
import os
import re
import sys

_ARGV_LIMIT = 4 * 1024 * 1024
_MAX_DESCENDANTS = 128
_DEVTOOLS = re.compile(r"(?:^|/)chrome-devtools-mcp(?:@[^/\s]*)?(?:/|$)")
_CODEX_BINARY = re.compile(r"codex-(?:aarch64|arm64|x86_64|x64)-(?:apple-darwin|unknown-linux-(?:gnu|musl))(?:\.exe)?\Z")
_CLAUDE_VERSION = re.compile(r"\d+\.\d+\.\d+\Z")
PROCESS_UNVERIFIED = "Agent process could not be verified"
PROCESS_INCOMPLETE = "Some process arguments could not be read"
RELAY_UNVERIFIED = "Screen relay mode could not be verified"
BROWSER_UNVERIFIED = "Chrome launch flag was not recorded"
UNKNOWN_REASONS = frozenset({PROCESS_UNVERIFIED, PROCESS_INCOMPLETE, RELAY_UNVERIFIED, BROWSER_UNVERIFIED})


@lru_cache(maxsize=1)
def _darwin_argv_reader():
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    argmax = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(argmax))
    if libc.sysctl((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(argmax), ctypes.byref(size), None, 0):
        return None  # CTL_KERN, KERN_ARGMAX
    return libc, min(max(argmax.value, 4096), _ARGV_LIMIT)


def _darwin_argv(pid):
    reader = _darwin_argv_reader()
    if reader is None:
        return None
    libc, capacity = reader
    size = ctypes.c_size_t(capacity)
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl((ctypes.c_int * 3)(1, 49, pid), 3, buffer, ctypes.byref(size), None, 0):
        return None  # CTL_KERN, KERN_PROCARGS2
    raw = ctypes.string_at(buffer, size.value)
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
        if value == "--":
            break
        if value in ("--chrome", "--no-chrome"):
            flag = value == "--chrome"
    return flag


def matches_harness(values, kind):
    """A generic Node process or shell in the pane does not identify the CLI."""
    if not values:
        return False
    path = values[0]
    binary = os.path.basename(path).lower()
    if kind == "claude":
        return (binary == "claude"
                or bool(_CLAUDE_VERSION.fullmatch(binary) and "/claude/" in path)
                or (binary == "node" and len(values) > 1
                    and "/@anthropic-ai/claude-code/" in values[1]))
    if kind == "codex":
        return binary == "codex" or bool(_CODEX_BINARY.fullmatch(binary))
    return False


def _read_descendants(pid, process_tree, read):
    names, children = process_tree
    seen, pending = {pid}, list(children.get(pid, ()))
    processes, incomplete = [], False
    while pending:
        child = pending.pop()
        if child in seen:
            continue
        if len(seen) > _MAX_DESCENDANTS:
            return processes, True
        seen.add(child)
        values = read(child) if child in names else None
        if values:
            processes.append(values)
            # The relay's own driver is not an independent control route.
            # An unreadable parent also cannot establish an independent route.
            if not is_screen(values):
                pending.extend(children.get(child, ()))
        else:
            incomplete = True
    return processes, incomplete


def observe(pid, process_tree, kind, *, read=None):
    """Observation only. A missing argument list cannot establish Off."""
    read = read or argv
    own = read(pid) if pid is not None and process_tree is not None and pid in process_tree[0] else None
    result = {"permission_source": "process", "computer_use": None, "browser_use": None}
    if not matches_harness(own, kind):
        return dict(result, computer_use_reason=PROCESS_UNVERIFIED, browser_use_reason=PROCESS_UNVERIFIED)
    descendants, incomplete = _read_descendants(pid, process_tree, read)
    if any(is_cua(values) for values in descendants):
        result["computer_use"] = "control"
    elif any(is_screen(values) for values in descendants):
        # Seeing a relay does not identify its current binding or mode.
        result["computer_use_reason"] = RELAY_UNVERIFIED
    elif incomplete:
        result["computer_use_reason"] = PROCESS_INCOMPLETE
    else:
        result["computer_use"] = "off"
    if kind == "claude":
        result["browser_use"] = chrome_flag(own)
        if result["browser_use"] is None:
            result["browser_use_reason"] = BROWSER_UNVERIFIED
    elif any(is_devtools(values) for values in descendants):
        result["browser_use"] = True
    elif incomplete:
        result["browser_use_reason"] = PROCESS_INCOMPLETE
    else:
        result["browser_use"] = False
    return result
