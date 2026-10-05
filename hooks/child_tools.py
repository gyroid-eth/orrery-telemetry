#!/usr/bin/env python3
"""Which tools a child agent is given: the ``base`` and ``tools`` selection.

A selection has two parts:

``base``
    ``"default"`` keeps the launch as it was before this option existed:
    a Claude child gets ORRERY Mail as its only configured MCP server, while
    Claude in Chrome and the built-in macOS computer use still follow the
    user's own settings and the child's working directory; a Codex child
    inherits the user's MCP servers. ``"mail-only"`` promises that the child
    gets nothing beyond ORRERY Mail except what ``tools`` names.

``tools``
    ``browser`` (Claude in Chrome, optionally a deviceId), ``screen`` (the
    whole screen, ``read`` or ``operate``; either the built-in macOS computer
    use of Claude Code or a user MCP server such as Windows-MCP), ``mcp``
    (user MCP servers chosen by name) and ``approve_all`` (servers in ``mcp``
    whose every tool is approved; the CLI form is ``mcp:<server>:all``).

Selecting anything (``mail-only`` or any tool) makes the launch fail closed:
if the selection cannot be applied exactly, the child is not started. This
is a policy for the child, not an isolation boundary: the child runs as the
same user and can edit its own configuration.

What a screen selection exposes and approves is decided by an allow list,
the classification table TOOL_TABLE: for a known server and version it lists
the tools that only read the screen (``read``), the tools that operate the
screen (``operate``: click, type, open windows), and every tool it publishes
(``all``). Tools in neither class (PowerShell, Registry, files, processes,
clipboard, ...) are not screen tools. ``screen:read`` exposes and approves
``read``; ``screen:operate`` exposes and approves ``read`` + ``operate``;
everything else is hidden (Claude ``--disallowed-tools``, Codex
``enabled_tools``). Only the separate ``mcp:<server>:all`` approves every
tool of a known version, including ones that run arbitrary code on the host.

Approval stays inside the child's own launch, never in the user's settings:
a headless child would otherwise stop at the first approval prompt (Claude,
measured on WSL 2026-10-01) or fail under ``never`` (Codex). Claude gets
``--allowed-tools`` and Codex ``approval_mode = "approve"``, tool by tool and
only for tools the table knows. A whole server is never approved: a server of
unknown version, or one selected with plain ``mcp:<server>``, is copied or
enabled, and its tools still need the user's own approval. Because approval
comes only from the table, a resume after the server was upgraded to a version
the table does not know approves nothing rather than more.

Usage (the launcher and the dashboard):
  child_tools.py parse --provider P [--base B] [--tools SPEC]... [--chrome 0|1] [--chrome-device D]
  child_tools.py claude-plan --spec JSON --cwd DIR --chrome 0|1 [--claude-json PATH]
  child_tools.py claude-config --spec JSON --cwd DIR --out PATH --runner R --child NAME
      --project-key K --token-file T --mcp-url U --mail-env E --runtime-dir D
      --bearer-mode M [--python-bin P] [--parent-agent NAME] [--claude-json PATH]
  child_tools.py codex-record --spec JSON --out PATH
  child_tools.py prompt --spec JSON --provider P [--standalone 0|1]
  child_tools.py windows-session --spec JSON
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Any

BASES = ("default", "mail-only")
ACCESS = ("read", "operate")
TOOL_KINDS = ("browser", "screen", "mcp", "approve_all")
SERVER_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
DEVICE_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
# Claude Code's built-in computer use server (macOS only). It is enabled per
# project with /mcp, which records it in ~/.claude.json projects[<dir>].
BUILTIN_SCREEN = "computer-use"
CODEX_RECORD_VERSION = 1
MAX_RECORD_BYTES = 4096

# The classification table. Each entry lists the tools of one server version
# that only read the screen, those that operate the screen, and every tool it
# publishes. A tool in neither class is not a screen tool and is approved only
# by an explicit mcp:<server>:all. Source for Windows-MCP 0.8.6:
# src/windows_mcp/tools/*.py at tag v0.8.6 (CursorTouch/Windows-MCP), 20 tools;
# not screen: Clipboard, FileSystem, Notification, PowerShell, Process,
# Registry, Scrape.
TOOL_TABLE: dict[str, dict[str, dict[str, tuple[str, ...]]]] = {
    "windows-mcp": {
        "0.8.6": {
            "read": ("Screenshot", "Snapshot"),
            "operate": (
                "App", "Click", "DisplayInventory", "Move", "MultiEdit",
                "MultiSelect", "Scroll", "Shortcut", "Type", "Wait", "WaitFor",
            ),
            "all": (
                "App", "Click", "Clipboard", "DisplayInventory", "FileSystem",
                "Move", "MultiEdit", "MultiSelect", "Notification", "PowerShell",
                "Process", "Registry", "Scrape", "Screenshot", "Scroll",
                "Shortcut", "Snapshot", "Type", "Wait", "WaitFor",
            ),
        },
    },
}
# Browser MCP: chrome-devtools-mcp. Source: the tools/list of 1.10.1 (30
# tools, read with an MCP initialize + tools/list on 2026-10-05). The real
# definition is usually unpinned (``npx chrome-devtools-mcp@latest``), so a
# version that is not listed, or no version at all, is classified by tool NAME
# (the "*" entry): only the names below are exposed and approved, one by one,
# and a tool a newer version adds is neither exposed nor approved. "all" is
# listed only for a pinned version, so ``mcp:<server>:all`` still needs one.
# Browser / operate explicitly approves all known names, including arbitrary
# page JS (evaluate_script and navigate_page.initScript), host file output and
# upload. Read excludes every tool with a host output-path argument: approval
# cannot distinguish its arguments.
_CHROME_DEVTOOLS_READ = (
    "get_console_message", "get_css_styles",
    "list_console_messages", "list_network_requests", "list_pages",
    "performance_analyze_insight", "select_page", "wait_for",
)
_CHROME_DEVTOOLS_OPERATE = (
    "click", "close_page", "drag", "emulate", "fill", "fill_form",
    "handle_dialog", "hover", "navigate_page", "new_page", "press_key",
    "resize_page", "type_text",
    "evaluate_script", "upload_file", "take_heapsnapshot",
    "performance_start_trace", "performance_stop_trace", "lighthouse_audit",
    "get_network_request", "take_screenshot", "take_snapshot",
)
TOOL_TABLE["chrome-devtools-mcp"] = {
    "*": {"read": _CHROME_DEVTOOLS_READ, "operate": _CHROME_DEVTOOLS_OPERATE},
    "1.10.1": {
        "read": _CHROME_DEVTOOLS_READ,
        "operate": _CHROME_DEVTOOLS_OPERATE,
        "all": tuple(sorted(_CHROME_DEVTOOLS_READ + _CHROME_DEVTOOLS_OPERATE)),
    },
}
# Packages whose unpinned or unlisted-version definition is classified by name.
NAME_CLASSIFIED = ("chrome-devtools-mcp",)
_PACKAGE_VERSION_RE = re.compile(
    r"(?:^|[/\\\s])([A-Za-z0-9_.-]+?)(?:@|==)(\d+(?:\.\d+){1,3})$")


class ToolsError(ValueError):
    """The selection is invalid or cannot be applied as requested."""


def looks_like_agent_mail(name: str) -> bool:
    normalized = name.replace("-", "").replace("_", "").replace('"', "").lower()
    return normalized in {"agentmail", "mcpagentmail", "agentstackmail", "orrerymail"}


def _server_name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not SERVER_RE.fullmatch(value):
        raise ToolsError(f"{what} must match [A-Za-z0-9_-]{{1,64}}")
    if looks_like_agent_mail(value):
        raise ToolsError(
            f"{what} {value!r} is ORRERY Mail; the child always gets its own "
            "authenticated Mail, so do not select it")
    return value


# --------------------------------------------------------------------------- #
# the selection
# --------------------------------------------------------------------------- #
def normalize(base: Any = None, tools: Any = None) -> dict:
    """Validate an API/CLI selection and return {"base", "tools"}.

    ``tools`` holds only the kinds that were selected: ``browser`` as
    {"device": str}, ``screen`` as {"access", "server"} (server None means the
    built-in computer use) and ``mcp`` as a sorted list of server names."""
    if base is None:
        base = "default"
    if base not in BASES:
        raise ToolsError("base must be \"default\" or \"mail-only\"")
    if tools is None:
        tools = {}
    if not isinstance(tools, dict):
        raise ToolsError("tools must be an object")
    unknown = sorted(set(tools) - set(TOOL_KINDS))
    if unknown:
        raise ToolsError(f"unknown tools: {', '.join(unknown)}")
    result: dict[str, Any] = {}

    if "browser" in tools:
        browser = tools["browser"]
        if browser is True:
            browser = {}
        if not isinstance(browser, dict) or set(browser) - {"device"}:
            raise ToolsError("tools.browser must be true or {\"device\": \"<deviceId>\"}")
        device = browser.get("device", "")
        if not isinstance(device, str):
            raise ToolsError("tools.browser.device must be a string")
        device = device.strip()
        if device and not DEVICE_RE.fullmatch(device):
            raise ToolsError("tools.browser.device must match [A-Za-z0-9._:-]{1,128}")
        result["browser"] = {"device": device}

    if "screen" in tools:
        screen = tools["screen"]
        if isinstance(screen, str):
            screen = {"access": screen}
        if not isinstance(screen, dict) or set(screen) - {"access", "server"}:
            raise ToolsError(
                "tools.screen must be \"read\", \"operate\" or "
                "{\"access\": ..., \"server\": \"<name>\"}")
        access = screen.get("access")
        if access not in ACCESS:
            raise ToolsError("tools.screen.access must be \"read\" or \"operate\"")
        server = screen.get("server")
        if server is not None:
            server = _server_name(server, "tools.screen.server")
        result["screen"] = {"access": access, "server": server}

    if "mcp" in tools:
        names = tools["mcp"]
        if not isinstance(names, list) or not names:
            raise ToolsError("tools.mcp must be a non-empty list of server names")
        checked = [_server_name(name, "tools.mcp entry") for name in names]
        if len(set(checked)) != len(checked):
            raise ToolsError("tools.mcp lists a server twice")
        screen_server = (result.get("screen") or {}).get("server")
        if screen_server in checked:
            raise ToolsError(
                f"{screen_server!r} is already selected as the screen server; "
                "list it only once")
        result["mcp"] = sorted(checked)

    if "approve_all" in tools:
        names = tools["approve_all"]
        if not isinstance(names, list) or not names:
            raise ToolsError("tools.approve_all must be a non-empty list of server names")
        checked = [_server_name(name, "tools.approve_all entry") for name in names]
        if len(set(checked)) != len(checked) or not set(checked) <= set(result.get("mcp", ())):
            raise ToolsError(
                "each tools.approve_all server must be listed once and also in tools.mcp")
        result["approve_all"] = sorted(checked)
    return {"base": base, "tools": result}


def parse_cli(values: list[str]) -> dict:
    """Turn /delegate --tools values into a ``tools`` object.

    Accepted forms (comma separated or repeated): ``browser``,
    ``browser:<deviceId>``, ``screen`` (= operate), ``screen:read``,
    ``screen:operate``, ``screen:<read|operate>:<server>``, ``mcp:<server>``
    and ``mcp:<server>:all`` (also approve every tool of that server)."""
    tools: dict[str, Any] = {}
    for value in values:
        for item in (part.strip() for part in value.split(",")):
            if not item:
                raise ToolsError("empty --tools entry")
            kind, _, rest = item.partition(":")
            if kind == "browser":
                if "browser" in tools:
                    raise ToolsError("--tools browser given twice")
                tools["browser"] = {"device": rest}
            elif kind == "screen":
                if "screen" in tools:
                    raise ToolsError("--tools screen given twice")
                access, _, server = rest.partition(":")
                screen: dict[str, Any] = {"access": access or "operate"}
                if server:
                    screen["server"] = server
                tools["screen"] = screen
            elif kind == "mcp":
                name, _, approve = rest.partition(":")
                if not name:
                    raise ToolsError("--tools mcp needs a server name: mcp:<name>")
                if approve not in ("", "all"):
                    raise ToolsError("--tools mcp:<name> takes only :all after the name")
                tools.setdefault("mcp", []).append(name)
                if approve:
                    tools.setdefault("approve_all", []).append(name)
            else:
                raise ToolsError(
                    f"unknown --tools entry {item!r} (browser[:deviceId], "
                    "screen[:read|:operate[:server]], mcp:<server>[:all])")
    return tools


def restrictive(spec: dict) -> bool:
    """True when the launch must fail closed."""
    return spec["base"] == "mail-only" or bool(spec["tools"])


def check_provider(spec: dict, provider: str) -> None:
    tools = spec["tools"]
    if provider == "codex":
        browser = tools.get("browser")
        if browser is not None and browser["device"]:
            raise ToolsError(
                "a deviceId selects a Claude in Chrome browser; a Codex child "
                "uses the user's chrome-devtools MCP server, so give plain "
                "\"browser\" without a deviceId")
        screen = tools.get("screen")
        if screen and screen["server"] is None:
            raise ToolsError(
                "a Codex child has no built-in screen tool; name the user's "
                "screen server (screen:<read|operate>:<server>)")
    elif provider != "claude":
        if restrictive(spec):
            raise ToolsError(f"base and tools are not supported for provider: {provider}")


def merge_chrome(spec: dict, chrome: bool, device: str) -> tuple[bool, str]:
    """Combine the older claude_chrome fields with tools.browser.

    Both may be given only when they agree on the deviceId."""
    browser = spec["tools"].get("browser")
    if browser is None:
        return chrome, device
    if chrome and device and browser["device"] and device != browser["device"]:
        raise ToolsError(
            "claude_chrome_device and tools.browser.device name different browsers")
    return True, browser["device"] or device


def _direct_chrome_package(definition: dict) -> str | None:
    """Recognize a direct npx package invocation, never an incidental argument.

    Only launcher flags that do not change the executed package are accepted.
    In particular, --package/-p and -c may run an entirely different program.
    """
    command = definition.get("command")
    args = definition.get("args") or []
    if not isinstance(command, str) or not isinstance(args, list):
        return None
    if re.split(r"[/\\]", command)[-1] not in ("npx", "npx.cmd", "npx.exe"):
        return None
    options = True
    for word in args:
        if options and word == "--":
            options = False
            continue
        if options and word in ("-y", "--yes", "--no"):
            continue
        if not isinstance(word, str):
            return None
        if re.fullmatch(r"chrome-devtools-mcp(?:@[A-Za-z0-9_.-]+)?", word):
            return word
        return None
    return None


def table_entry(definition: dict) -> tuple[str, str, dict] | None:
    """The classification-table entry for a server definition, if known.

    The package and version are read from the command line (``pkg@1.2.3`` or
    ``pkg==1.2.3``, e.g. ``uvx windows-mcp@0.8.6``). A wrapper script or an
    unpinned package is not in the table, except for a direct npx invocation
    of chrome-devtools-mcp, classified by name. An incidental dependency
    (``uvx --with pkg@1.2.3 other``) is not the executed server."""
    chrome_package = _direct_chrome_package(definition)
    if chrome_package is not None:
        package = "chrome-devtools-mcp"
        version = chrome_package.partition("@")[2]
        versions = TOOL_TABLE[package]
        version = version if version in versions else "*"
        return package, version, versions[version]
    words = [definition.get("command")] + list(definition.get("args") or [])
    for index, word in enumerate(words):
        if not isinstance(word, str):
            continue
        if index > 0 and words[index - 1] in ("--with", "-w"):
            continue
        match = _PACKAGE_VERSION_RE.search(word.strip())
        if not match:
            continue
        package, version = match.group(1).lower(), match.group(2)
        if package in NAME_CLASSIFIED:
            continue
        entry = TOOL_TABLE.get(package, {}).get(version)
        if entry is not None:
            return package, version, entry
    return None


def _describe(spec: dict, provider: str = "claude") -> list[str]:
    tools = spec["tools"]
    parts = []
    browser = tools.get("browser")
    if browser is not None:
        parts.append("browser (Claude in Chrome"
                     + (f", deviceId {browser['device']})" if browser["device"] else ")")
                     if provider != "codex" else "browser (the chrome-devtools MCP server)")
    screen = tools.get("screen")
    if screen is not None:
        where = f"MCP server {screen['server']}" if screen["server"] else "computer use"
        parts.append(f"whole screen, {screen['access']} ({where})")
    approve_all = set(tools.get("approve_all", ()))
    for name in tools.get("mcp", ()):
        parts.append(f"MCP server {name}"
                     + (" (every tool approved)" if name in approve_all else ""))
    return parts


def _approval(spec: dict, name: str, definition: dict,
              browser_server: str | None = None) -> tuple[list[str], list[str]]:
    """(tools to expose and approve, tools to hide) for one selected server.

    Empty lists mean: copy/enable the server, approve nothing, hide nothing."""
    tools = spec["tools"]
    screen = tools.get("screen")
    if name == browser_server and (screen is None or screen["server"] != name):
        # A Codex child's browser: the user's browser MCP server, operated.
        screen = {"access": "operate", "server": name}
    entry = table_entry(definition)
    if name in tools.get("approve_all", ()):
        if entry is None or "all" not in entry[2]:
            raise ToolsError(
                f"MCP server {name!r} is not a server version in the classification table, so ORRERY "
                "cannot list its tools to approve them all (mcp:<name>:all); select "
                "it without :all and approve its tools in your own settings")
        return list(entry[2]["all"]), []
    if screen is None or screen["server"] != name:
        return [], []
    if entry is None:
        if screen["access"] == "read":
            raise ToolsError(
                f"MCP server {name!r} is not a server version in the classification table (the "
                "command line must pin it, e.g. windows-mcp@0.8.6), so it cannot be "
                "given read only; screen:operate gives it with no tool approved")
        return [], []
    table = entry[2]
    exposed = list(table["read"])
    if screen["access"] == "operate":
        exposed += list(table["operate"])
    return exposed, [tool for tool in table.get("all", ()) if tool not in exposed]


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #
def _load_claude_settings(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            settings = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ToolsError(f"cannot read {path}: {exc}") from exc
    return settings if isinstance(settings, dict) else {}


def _project_entry(settings: dict, cwd: str) -> dict:
    projects = settings.get("projects")
    if not isinstance(projects, dict):
        return {}
    for key in dict.fromkeys((cwd, os.path.abspath(cwd), os.path.realpath(cwd))):
        entry = projects.get(key)
        if isinstance(entry, dict):
            return entry
    return {}


def computer_use_enabled(settings: dict, cwd: str) -> bool:
    """Whether Claude Code offers its built-in computer use in this directory.

    Measured 2026-09-30 (Claude Code 2.1.285): the server reaches a child only
    when the child's project has it in enabledMcpServers."""
    project = _project_entry(settings, cwd)
    enabled = project.get("enabledMcpServers")
    disabled = project.get("disabledMcpServers")
    return (isinstance(enabled, list) and BUILTIN_SCREEN in enabled
            and not (isinstance(disabled, list) and BUILTIN_SCREEN in disabled))


def _copyable_definition(name: str, settings: dict, cwd: str) -> dict:
    """The user's definition of one server, checked so a copy works and holds no secret."""
    project = _project_entry(settings, cwd).get("mcpServers")
    user = settings.get("mcpServers")
    definition = None
    for scope in (project, user):
        if isinstance(scope, dict) and name in scope:
            definition = scope[name]
            break
    if definition is None:
        raise ToolsError(
            f"MCP server {name!r} is not in ~/.claude.json (user scope or the "
            "project of the child's directory). Servers from a project .mcp.json, "
            "a plugin or claude.ai connectors cannot be given to a child")
    if not isinstance(definition, dict):
        raise ToolsError(f"MCP server {name!r} has an invalid definition")
    for field in ("env", "headers"):
        if definition.get(field):
            raise ToolsError(
                f"MCP server {name!r} has {field} values; copying them would put "
                "credentials in the child's config, so it cannot be selected")
    kind = definition.get("type", "stdio")
    if kind == "stdio":
        command = definition.get("command")
        if not isinstance(command, str) or not os.path.isabs(command):
            raise ToolsError(
                f"MCP server {name!r} has a command that is not an absolute path "
                f"({command!r}); it would depend on the child's PATH and directory")
        cwd_field = definition.get("cwd")
        if cwd_field is not None and (not isinstance(cwd_field, str)
                                      or not os.path.isabs(cwd_field)):
            raise ToolsError(f"MCP server {name!r} has a relative cwd")
    elif not isinstance(definition.get("url"), str):
        raise ToolsError(f"MCP server {name!r} has no url")
    return dict(definition)


def claude_plan(spec: dict, *, cwd: str, chrome: bool, claude_json: str,
                platform: str | None = None) -> dict:
    """What a Claude child is launched with: extra servers and CLI flags.

    Raises ToolsError whenever the selection cannot be honoured."""
    platform = platform or sys.platform
    tools = spec["tools"]
    mail_only = spec["base"] == "mail-only"
    settings = _load_claude_settings(claude_json)
    servers: dict[str, dict] = {}
    flags: list[str] = []
    allowed: list[str] = []
    disallowed: list[str] = []

    screen = tools.get("screen")
    builtin_screen = screen is not None and screen["server"] is None
    if builtin_screen:
        if platform != "darwin":
            raise ToolsError(
                "the built-in computer use exists only on macOS; on this OS name "
                "the screen server: screen:<read|operate>:<server>")
        if screen["access"] == "read":
            raise ToolsError(
                "computer use is not in the classification table, so it can only be "
                "given as operate (screen:operate)")
        if not computer_use_enabled(settings, cwd):
            raise ToolsError(
                f"computer use is not enabled for {cwd}. Claude Code offers it only "
                "in projects where it was enabled with /mcp; enable it there or "
                "start the child in such a directory (a --worktree child has its "
                "own directory)")
    elif mail_only and platform == "darwin" and computer_use_enabled(settings, cwd):
        raise ToolsError(
            f"computer use is enabled for {cwd}, and there is not yet a verified "
            "way to keep it from a mail-only child. Start the child in another "
            "directory, or select screen:operate")

    names = list(tools.get("mcp", ()))
    if screen is not None and screen["server"] is not None:
        names.append(screen["server"])
    for name in names:
        servers[name] = _copyable_definition(name, settings, cwd)

    for name in names:
        if screen is not None and screen["server"] == name:
            entry = table_entry(servers[name])
            if entry is not None and entry[0] in NAME_CLASSIFIED and entry[1] == "*":
                raise ToolsError(
                    f"MCP server {name!r} must pin a version in the classification "
                    "table for Claude screen selection (e.g. chrome-devtools-mcp@1.10.1); "
                    "Claude cannot hide unknown tools with a name allowlist")
        exposed, hidden = _approval(spec, name, servers[name])
        allowed += [f"mcp__{name}__{tool}" for tool in exposed]
        disallowed += [f"mcp__{name}__{tool}" for tool in hidden]

    if mail_only and not chrome:
        flags.append("--no-chrome")
    if allowed:
        flags += ["--allowed-tools", ",".join(allowed)]
    if disallowed:
        flags += ["--disallowed-tools", ",".join(disallowed)]
    return {"servers": servers, "flags": flags}


def mail_alias_names(settings: dict) -> list[str]:
    """ORRERY Mail names the user already uses, all claimed by the child's proxy.

    The proxy is published under each name the user's settings use for a direct
    Mail connection: --mcp-config overrides a same-named server (measured), and
    the model otherwise reaches for the name it knows."""
    names = {"orrery-mail"}
    scopes = [settings.get("mcpServers")]
    projects = settings.get("projects")
    if isinstance(projects, dict):
        scopes += [scope.get("mcpServers") for scope in projects.values()
                   if isinstance(scope, dict)]
    for scope in scopes:
        if isinstance(scope, dict):
            names.update(name for name in scope
                         if isinstance(name, str) and looks_like_agent_mail(name))
    return sorted(names)


def claude_config(proxy: dict, *, claude_json: str, servers: dict | None = None) -> dict:
    """The child's --mcp-config: the Mail proxy under every Mail name, plus
    the selected servers. Used by both the launcher and the dashboard resume."""
    try:
        settings = _load_claude_settings(claude_json)
    except ToolsError:
        settings = {}
    config = {name: proxy for name in mail_alias_names(settings)}
    for name, definition in (servers or {}).items():
        if name in config:
            raise ToolsError(f"MCP server {name!r} collides with ORRERY Mail")
        config[name] = definition
    return {"mcpServers": config}


def write_private_json(path: str, payload: dict) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".child-tools.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #
def codex_browser_server(servers: dict) -> str:
    """The name of the user's browser MCP server in a Codex config.toml.

    ``chrome-devtools`` when it is there, else the one server whose command
    runs chrome-devtools-mcp."""
    found = [name for name, definition in servers.items()
             if isinstance(definition, dict)
             and name != "agentstack" and not looks_like_agent_mail(name)
             and (name == "chrome-devtools" or (
                 (table_entry(definition) or ("",))[0] in NAME_CLASSIFIED))]
    if "chrome-devtools" in found:
        return "chrome-devtools"
    if len(found) == 1:
        return found[0]
    raise ToolsError(
        "tools.browser for a Codex child needs the user's chrome-devtools MCP "
        "server in config.toml (a [mcp_servers.chrome-devtools] table, or one "
        "server that runs chrome-devtools-mcp)"
        + (": several match" if found else ""))


def codex_apply(config: dict, spec: dict) -> None:
    """Apply a selection to a child's parsed config.toml, in place.

    The base (mail-only = the orrery-only profile) is applied by the caller
    first; this enables the selected servers, exposes and approves only the
    table's screen tools of the screen server, and approves every tool of a
    server in approve_all. A server-wide approval is never written."""
    tools = spec["tools"]
    check_provider(spec, "codex")
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        servers = {}
    names = list(tools.get("mcp", ()))
    screen = tools.get("screen")
    if screen is not None:
        names.append(screen["server"])
    browser_server = None
    if "browser" in tools:
        browser_server = codex_browser_server(servers)
        entry = table_entry(servers[browser_server])
        if entry is None or entry[0] not in NAME_CLASSIFIED:
            raise ToolsError(
                f"MCP server {browser_server!r} does not run chrome-devtools-mcp "
                "directly (a wrapper script is not classified), so its tools "
                "cannot be approved for tools.browser; run it as "
                "npx chrome-devtools-mcp[@version]")
        if browser_server not in names:
            names.append(browser_server)
    for name in names:
        server = servers.get(name)
        if not isinstance(server, dict):
            raise ToolsError(
                f"MCP server {name!r} is not in the user's Codex config.toml")
        server["enabled"] = True
    for name in names:
        server = servers[name]
        exposed, hidden = _approval(spec, name, server, browser_server)
        if name == browser_server and name not in tools.get("approve_all", ()) \
                and not exposed:
            raise ToolsError(
                f"MCP server {name!r} does not run chrome-devtools-mcp "
                "directly (a wrapper script is not classified), so its tools "
                "cannot be approved for tools.browser; run it as "
                "npx chrome-devtools-mcp[@version]")
        restrict = name not in tools.get("approve_all", ()) and (
            name == browser_server
            or (screen is not None and screen["server"] == name))
        if hidden or (restrict and exposed):
            # A browser or screen selection: publish only those tools, approved.
            server["enabled_tools"] = exposed
            server.pop("default_tools_approval_mode", None)
            server["tools"] = {tool: {"approval_mode": "approve"} for tool in exposed}
            continue
        if exposed:
            tools_table = server.get("tools")
            if not isinstance(tools_table, dict):
                tools_table = server["tools"] = {}
            for tool in exposed:
                current = tools_table.get(tool)
                if not isinstance(current, dict):
                    current = tools_table[tool] = {}
                current["approval_mode"] = "approve"


def write_codex_record(path: str, spec: dict) -> None:
    write_private_json(path, {"version": CODEX_RECORD_VERSION, **spec})


def read_codex_record(path: str) -> dict | None:
    """The selection a Codex child was started with, None when it had none."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    import stat
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() \
            or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > MAX_RECORD_BYTES:
        raise ToolsError("the Codex tools record is not a private regular file")
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ToolsError("the Codex tools record is not valid JSON") from exc
    if not isinstance(record, dict) or record.get("version") != CODEX_RECORD_VERSION:
        raise ToolsError("the Codex tools record has an unknown schema")
    spec = normalize(record.get("base"), record.get("tools"))
    check_provider(spec, "codex")
    return spec


# --------------------------------------------------------------------------- #
# WSL: which Windows session will the screen server run in?
# --------------------------------------------------------------------------- #
def running_in_wsl(environ: dict | None = None) -> bool:
    environ = os.environ if environ is None else environ
    return bool(environ.get("WSL_DISTRO_NAME")) or os.path.exists(
        "/proc/sys/fs/binfmt_misc/WSLInterop")


def windows_session_warning(spec: dict, *, environ: dict | None = None,
                            timeout: float = 5.0) -> str:
    """A warning when a screen server on WSL would run without a desktop.

    Measured 2026-10-01: a Windows process started from WSL runs in the
    Windows session of the WSL instance. WSL started over ssh is in session 0
    (no desktop): Windows-MCP's Screenshot fails with "screen grab failed".
    Started from a tmux server inside an RDP session it runs in that session
    and works. The probe runs from the launcher's process; a child in a tmux
    server that was started elsewhere can be in another session, so this is
    a warning, not a stop."""
    screen = spec["tools"].get("screen")
    if screen is None or screen["server"] is None or not running_in_wsl(environ):
        return ""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "[System.Diagnostics.Process]::GetCurrentProcess().SessionId"],
            capture_output=True, text=True, timeout=timeout, check=False,
            stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ("Warning: could not ask Windows which session this WSL runs in "
                "(powershell.exe); if screenshots fail, start the tmux server "
                "from inside an RDP or console session, not over ssh.")
    session = result.stdout.strip()
    if session == "0":
        return ("Warning: this WSL runs in Windows session 0, which has no desktop "
                "(WSL was started over ssh or as a service). The screen server "
                f"{screen['server']!r} will start but screenshots fail (\"screen "
                "grab failed\"). Start the tmux server from inside an RDP or "
                "console session and launch the child there.")
    return ""


# --------------------------------------------------------------------------- #
# prompt
# --------------------------------------------------------------------------- #
def prompt_text(spec: dict, provider: str, standalone: bool = False) -> str:
    if not restrictive(spec):
        return ""
    report_to = "the operator" if standalone else "your parent agent"
    given = _describe(spec, provider)
    if spec["base"] == "mail-only":
        head = ("Tools: this child was started with base mail-only: besides "
                "shell/files and authenticated ORRERY Mail it was given only "
                + (", ".join(given) if given else "nothing else") + ".")
    else:
        head = ("Tools: in addition to the default ones, this child was given "
                + ", ".join(given) + ".")
    screen = spec["tools"].get("screen")
    if screen is not None and screen["access"] == "read":
        head += (" The whole-screen tools are read only: do not click, type or "
                 "open applications.")
    if spec["tools"].get("approve_all"):
        head += (" Every tool of " + ", ".join(spec["tools"]["approve_all"])
                 + " is approved, including tools that run arbitrary code on that "
                 "host: use only what the task needs.")
    if provider == "codex" and "browser" in spec["tools"]:
        head += (" Browser approves all known chrome-devtools tools, including "
                 "arbitrary JavaScript, saving host files and uploading local files.")
    return (head + " If a tool you need is missing, report it to " + report_to
            + " instead of working around it. This is an instruction, not a "
            "technical lock.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _spec_arg(text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ToolsError("--spec is not JSON") from exc
    if not isinstance(data, dict):
        raise ToolsError("--spec must be an object")
    return normalize(data.get("base"), data.get("tools"))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="child_tools.py")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("parse")
    p.add_argument("--provider", required=True)
    p.add_argument("--base", default=None)
    p.add_argument("--tools", action="append", default=[])
    p.add_argument("--chrome", default="0")
    p.add_argument("--chrome-device", default="")
    plan = sub.add_parser("claude-plan")
    plan.add_argument("--spec", required=True)
    plan.add_argument("--cwd", required=True)
    plan.add_argument("--chrome", default="0")
    plan.add_argument("--claude-json", default=os.path.expanduser("~/.claude.json"))
    cfg = sub.add_parser("claude-config")
    cfg.add_argument("--spec", default='{}')
    cfg.add_argument("--cwd", default="")
    cfg.add_argument("--chrome", default="0")
    cfg.add_argument("--claude-json", default=os.path.expanduser("~/.claude.json"))
    for name in ("--out", "--runner", "--child", "--project-key", "--token-file",
                 "--mcp-url", "--mail-env", "--runtime-dir", "--bearer-mode"):
        cfg.add_argument(name, required=True)
    cfg.add_argument("--python-bin", default="")
    cfg.add_argument("--parent-agent", default="")
    rec = sub.add_parser("codex-record")
    rec.add_argument("--spec", required=True)
    rec.add_argument("--out", required=True)
    pr = sub.add_parser("prompt")
    pr.add_argument("--spec", required=True)
    pr.add_argument("--provider", required=True)
    pr.add_argument("--standalone", default="0")
    ws = sub.add_parser("windows-session")
    ws.add_argument("--spec", required=True)
    args = parser.parse_args(argv[1:])
    try:
        if args.command == "parse":
            tools = parse_cli(args.tools)
            spec = normalize(args.base or None, tools)
            check_provider(spec, args.provider)
            chrome, device = merge_chrome(spec, args.chrome == "1", args.chrome_device)
            sys.stdout.write(json.dumps(spec, sort_keys=True, separators=(",", ":"))
                             + "\t" + ("1" if chrome else "0") + "\t" + device + "\n")
            return 0
        if args.command == "claude-plan":
            result = claude_plan(_spec_arg(args.spec), cwd=args.cwd,
                                 chrome=args.chrome == "1", claude_json=args.claude_json)
            sys.stdout.write(" ".join(result["flags"]) + "\n")
            return 0
        if args.command == "claude-config":
            spec = _spec_arg(args.spec)
            servers: dict = {}
            if restrictive(spec):
                servers = claude_plan(spec, cwd=args.cwd, chrome=args.chrome == "1",
                                      claude_json=args.claude_json)["servers"]
            env = dict(
                AGENTSTACK_PROXY_AGENT_NAME=args.child,
                AGENTSTACK_PROXY_TOKEN_FILE=args.token_file,
                AGENTSTACK_PROXY_PROGRAM="claude-code",
                AGENTSTACK_PROJECT_KEY=args.project_key,
                AGENTSTACK_MCP_URL=args.mcp_url,
                AGENTSTACK_MAIL_ENV=args.mail_env,
                # The MCP client starts the proxy with this table only, not the
                # child's shell environment (see spawn_child.sh, 2026-09-03).
                AGENTSTACK_MAIL_HTTP_BEARER_MODE=args.bearer_mode,
                AGENTSTACK_RUNTIME_DIR=args.runtime_dir,
            )
            if args.python_bin:
                env["AGENTSTACK_PYTHON"] = args.python_bin
            # The proxy reports this as lineage.parent_agent (standalone: none).
            if args.parent_agent:
                env["AGENTSTACK_PROXY_PARENT_AGENT"] = args.parent_agent
            proxy = dict(command=args.runner, args=[], env=env)
            write_private_json(args.out, claude_config(
                proxy, claude_json=args.claude_json, servers=servers))
            return 0
        if args.command == "codex-record":
            write_codex_record(args.out, _spec_arg(args.spec))
            return 0
        if args.command == "windows-session":
            warning = windows_session_warning(_spec_arg(args.spec))
            if warning:
                print(warning, file=sys.stderr)
            return 0
        if args.command == "prompt":
            sys.stdout.write(prompt_text(_spec_arg(args.spec), args.provider,
                                         args.standalone == "1"))
            return 0
    except ToolsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
