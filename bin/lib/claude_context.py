#!/usr/bin/env python3
"""Give a managed Claude TUI a status-line observer for its context usage."""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys


OPTION = "@orrery-claude-context"


def _settings(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Claude settings must be an object")
    return data


def _local_settings(cwd):
    """Look in the main repository for the local settings shared by worktrees."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"],
            cwd=cwd, capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return cwd / ".claude/settings.local.json"
    if result.returncode:
        return cwd / ".claude/settings.local.json"
    top, common = map(Path, result.stdout.splitlines())
    root = common.parent if common.name == ".git" else top
    if (root == Path.home() or root.stat().st_uid != os.getuid()
            or common.stat().st_uid != os.getuid()
            or (root / ".claude").exists() and (root / ".claude").stat().st_uid != os.getuid()):
        root = cwd
    return root / ".claude/settings.local.json"


def launch_args(command, pane, pid):
    """Wrap the effective command; keep the original argv on unsupported settings."""
    if not re.fullmatch(r"%[0-9]+", pane) or "--safe-mode" in command[1:]:
        return command
    flags = {}
    for index, arg in enumerate(command[1:], 1):
        if arg == "--":
            break
        key, equal, value = arg.partition("=")
        if key not in {"--settings", "--setting-sources"}:
            continue
        end = index + (1 if equal else 2)
        if key in flags or end > len(command):
            return command
        flags[key] = (index, end, value if equal else command[index + 1])

    start, end, inline = flags.get("--settings", (1, 1, "{}"))
    try:
        # File-based overrides can contain private settings. Leave the original
        # file to Claude instead of copying its contents into the process argv.
        settings = json.loads(inline)
        if not isinstance(settings, dict):
            return command
        sources = set(flags.get("--setting-sources", (0, 0, "user,project,local"))[2].split(","))
        cwd = Path.cwd()
        layers = []
        if "user" in sources:
            home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
            layers.append(home / "settings.json")
        if "project" in sources:
            layers.append(cwd / ".claude/settings.json")
        if "local" in sources:
            layers.append(_local_settings(cwd))
        status, status_source = {}, None
        for path in layers:
            layer = _settings(path)
            if layer.get("disableAllHooks") is True:
                return command
            if "statusLine" in layer:
                status, status_source = layer["statusLine"], path
        if settings.get("disableAllHooks") is True:
            return command
        if "statusLine" in settings:
            status, status_source = settings["statusLine"], None
    except (OSError, ValueError, RecursionError):
        return command

    if not isinstance(status, dict):
        return command
    original = status.get("command", "")
    if status and (status.get("type") != "command" or not isinstance(original, str)):
        return command
    if shlex.quote(str(Path(__file__).resolve())) in original:
        return command
    observer = shlex.join([sys.executable, str(Path(__file__).resolve()), "observe", pane, str(pid)])
    if original:
        if status_source is not None:
            observer += " --file " + shlex.quote(str(status_source))
        else:
            observer += " --command " + shlex.quote(original)
    settings["statusLine"] = {**status, "type": "command", "command": observer}
    return [*command[:start], "--settings", json.dumps(settings, separators=(",", ":")), *command[end:]]


def observe(raw, pane, pid):
    """Store numeric fields only. The tmux option dies with its pane."""
    if not os.environ.get("TMUX") or not re.fullmatch(r"%[0-9]+", pane) or not pid.isdecimal():
        return
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return
    window = payload.get("context_window") if isinstance(payload, dict) else None
    if not isinstance(window, dict):
        return
    used = window.get("used_percentage")
    capacity = window.get("context_window_size")
    percent = str(int(used + 0.5)) if type(used) in (int, float) and 0 <= used <= 100 else ""
    tokens = str(capacity) if type(capacity) is int and 0 < capacity < 10**12 else ""
    try:
        subprocess.run(
            ["tmux", "set-option", "-p", "-q", "-t", pane, OPTION, f"{pid}:{percent}:{tokens}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def main(args):
    if len(args) >= 2 and args[0] == "launch":
        pane = os.environ.get("TMUX_PANE", "") if os.environ.get("TMUX") else ""
        command = launch_args(args[1:], pane, os.getpid())
        try:
            os.execvp(command[0], command)
        except OSError as exc:
            print(exc, file=sys.stderr)
            return 127
    if len(args) in (3, 5) and args[0] == "observe":
        raw = sys.stdin.buffer.read()
        observe(raw, args[1], args[2])
        if len(args) == 5:
            command = args[4]
            if args[3] == "--file":
                try:
                    status = _settings(Path(command)).get("statusLine", {})
                except (OSError, ValueError, RecursionError):
                    return 0
                command = status.get("command") if isinstance(status, dict) else None
            elif args[3] != "--command":
                return 2
            if isinstance(command, str) and command:
                return subprocess.run(command, shell=True, input=raw).returncode
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
