#!/usr/bin/env python3
"""Launch record and browser policy for Claude children started with --chrome.

spawn_child.sh writes one record per child that asked for Claude in Chrome
(``--claude-chrome``); dashboard ``do_resume`` and session-start-reminder.sh read
it back. The record holds no credential.

The deviceId in the record is a selection policy the child is told to follow,
not a technical binding: Claude Code has no flag that pins Claude in Chrome to
one browser. The policy text says so, and it is repeated at every session start
(startup, resume, compaction) because a resumed transcript alone may carry an
older selection.

Usage:
  claude_chrome_policy.py write <record> <agent> <device|""> <standalone 0|1>
  claude_chrome_policy.py clear <record>
  claude_chrome_policy.py prompt <device|""> <standalone 0|1>
  claude_chrome_policy.py session <record> <agent>
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile

VERSION = 1
DEVICE_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
MAX_BYTES = 4096


class RecordError(ValueError):
    pass


def valid_device(device: str) -> bool:
    return device == "" or DEVICE_RE.fullmatch(device) is not None


def policy_text(device: str, standalone: bool) -> str:
    report_to = "the operator" if standalone else "your parent agent"
    if device:
        return (
            "Browser: this session was started with Claude in Chrome (--chrome). "
            f"The browser to use is deviceId {device}. Until that browser is "
            "selected, use no browser tool except list_connected_browsers. To "
            "select it: call list_connected_browsers, confirm that deviceId "
            f"{device} is listed and connected, call select_browser with exactly "
            "that deviceId, and confirm it succeeded. Only then open your own tab "
            "with tabs_create_mcp and work only in tabs you created after that "
            "selection; do not reuse an earlier selection or tab. Select again "
            "after any resume, restart, compaction or disconnect. If that "
            "deviceId is missing or disconnected, stop browser work and report "
            f"it to {report_to}. Never fall back to another browser (not the "
            "first one listed, not the local one, not one with a similar name "
            "or OS). This is an instruction for you to follow, not a technical "
            "lock."
        )
    return (
        "Browser: this session was started with Claude in Chrome (--chrome), "
        "but no target browser was specified. Use no browser tool except "
        "list_connected_browsers: do not select or switch browsers, open tabs, "
        "navigate, read pages or click. If the task needs a browser, send the "
        "names and deviceIds from list_connected_browsers to "
        f"{report_to} and wait for the deviceId to use."
    )


def read_record(path: str, agent: str) -> dict | None:
    """Return the validated record, None when absent, RecordError when bad."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RecordError(f"unreadable: {exc.strerror}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise RecordError("not a regular file")
    if info.st_uid != os.getuid():
        raise RecordError("not owned by this user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RecordError("permissions must be 0600")
    if info.st_size > MAX_BYTES:
        raise RecordError("too large")
    try:
        with open(path, "rb") as handle:
            state = json.loads(handle.read(MAX_BYTES + 1).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecordError("not valid JSON") from exc
    if not isinstance(state, dict) or state.get("version") != VERSION:
        raise RecordError("unknown schema")
    if state.get("agent_name") != agent:
        raise RecordError("recorded for a different agent")
    if state.get("claude_chrome") is not True:
        raise RecordError("claude_chrome is not true")
    device = state.get("chrome_device")
    if not isinstance(device, str) or not valid_device(device):
        raise RecordError("invalid deviceId")
    if not isinstance(state.get("standalone"), bool):
        raise RecordError("invalid standalone flag")
    return state


def write_record(path: str, agent: str, device: str, standalone: bool) -> None:
    if not valid_device(device):
        raise RecordError("invalid deviceId")
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".claude-launch.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({
                "version": VERSION,
                "agent_name": agent,
                "claude_chrome": True,
                "chrome_device": device,
                "standalone": standalone,
            }, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    command, args = argv[1], argv[2:]
    try:
        if command == "write" and len(args) == 4:
            write_record(args[0], args[1], args[2], args[3] == "1")
            return 0
        if command == "clear" and len(args) == 1:
            try:
                os.unlink(args[0])
            except FileNotFoundError:
                pass
            return 0
        if command == "prompt" and len(args) == 2:
            if not valid_device(args[0]):
                raise RecordError("invalid deviceId")
            sys.stdout.write(policy_text(args[0], args[1] == "1"))
            return 0
        if command == "session" and len(args) == 2:
            # A session-start hook must never fail the session; a bad record
            # is reported to the model as "do not use the browser" instead.
            try:
                state = read_record(args[0], args[1])
            except RecordError as exc:
                print(
                    "Browser: this session's Claude in Chrome launch record is "
                    f"invalid ({exc}), so the target browser is unknown. Use no "
                    "browser tool, and report this to your parent agent or the "
                    "operator."
                )
                return 0
            if state is not None:
                print(policy_text(state["chrome_device"], state["standalone"]))
            return 0
    except (OSError, RecordError) as exc:
        print(f"claude_chrome_policy: {command}: {exc}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
