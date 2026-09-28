#!/usr/bin/env python3
"""Launch records and browser policy for Claude children started with --chrome.

A record says "this Claude conversation was started with --chrome, and this is
the browser it was told to use". It holds no credential.

Records are bound to one conversation, never to an agent name alone, so a later
launch under the same name cannot change what an earlier conversation resumes
with. The lifecycle is:

1. spawn_child.sh writes ``<agent>.claude-launch.pending-<launch_id>.json``
   before starting tmux and passes ``AGENTSTACK_CLAUDE_LAUNCH_ID`` to the
   child. Nothing that already exists is modified; a failed launch only leaves
   a pending record that no session will ever claim.
2. The child's first SessionStart (session-start-reminder.sh) finds the
   pending record whose launch id matches its own environment and copies it to
   ``<agent>.claude-launch.<session_id>.json``. A later /clear in the same
   process gets a new session id and is bound the same way.
3. dashboard ``do_resume`` looks up the record for the exact session id it is
   about to resume (``claude --resume`` keeps the id) and passes its launch id
   on, so a /clear after the resume is bound to the same launch.
4. The policy text carries a launch-id marker, so the transcript itself shows
   that a conversation was started with --chrome. A conversation with the
   marker but no bound record (the SessionStart write failed) is not mistaken
   for one started without --chrome: resume binds it from the launch's record,
   or stops when that record is gone.

The deviceId is a selection policy the child is told to follow, not a
technical binding: Claude Code has no flag that pins Claude in Chrome to one
browser. The policy is printed at every session start (startup, resume,
compaction) because a resumed transcript alone may carry an older selection.

Usage:
  claude_chrome_policy.py prepare <dir> <agent> <launch_id> <device|""> <standalone 0|1>
  claude_chrome_policy.py prompt <device|""> <standalone 0|1> <launch_id>
  claude_chrome_policy.py session <dir> <agent> <session_id> <launch_id|"">
"""
from __future__ import annotations

import glob
import json
import os
import re
import stat
import sys
import tempfile

VERSION = 2
DEVICE_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
TOKEN_RE = re.compile(r"[A-Za-z0-9-]{8,128}")
AGENT_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
MAX_BYTES = 4096


class RecordError(ValueError):
    pass


def valid_device(device: str) -> bool:
    return device == "" or DEVICE_RE.fullmatch(device) is not None


def _check_names(agent: str, *tokens: str) -> None:
    if not AGENT_RE.fullmatch(agent or ""):
        raise RecordError("invalid agent name")
    for token in tokens:
        if not TOKEN_RE.fullmatch(token or ""):
            raise RecordError("invalid session or launch id")


def pending_path(directory: str, agent: str, launch_id: str) -> str:
    _check_names(agent, launch_id)
    return os.path.join(directory, f"{agent}.claude-launch.pending-{launch_id}.json")


def session_path(directory: str, agent: str, session_id: str) -> str:
    _check_names(agent, session_id)
    return os.path.join(directory, f"{agent}.claude-launch.{session_id}.json")


MARKER = "Claude in Chrome launch id: "
MARKER_RE = re.compile(re.escape(MARKER) + r"([A-Za-z0-9-]{8,128})")


def policy_text(device: str, standalone: bool, launch_id: str = "") -> str:
    marker = f" ({MARKER}{launch_id})" if launch_id else ""
    return _policy_body(device, standalone) + marker


def _policy_body(device: str, standalone: bool) -> str:
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
    launch_id = state.get("launch_id")
    if not isinstance(launch_id, str) or not TOKEN_RE.fullmatch(launch_id):
        raise RecordError("invalid launch id")
    session_id = state.get("session_id")
    if session_id is not None and (
        not isinstance(session_id, str) or not TOKEN_RE.fullmatch(session_id)
    ):
        raise RecordError("invalid session id")
    return state


def session_record(directory: str, agent: str, session_id: str) -> dict | None:
    """The record bound to exactly this conversation, if any."""
    path = session_path(directory, agent, session_id)
    state = read_record(path, agent)
    if state is not None and state.get("session_id") != session_id:
        raise RecordError("recorded for a different session")
    return state


def _write(path: str, state: dict) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".claude-launch.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
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


def prepare(directory: str, agent: str, launch_id: str, device: str,
            standalone: bool) -> None:
    if not valid_device(device):
        raise RecordError("invalid deviceId")
    _write(pending_path(directory, agent, launch_id), {
        "version": VERSION,
        "agent_name": agent,
        "launch_id": launch_id,
        "claude_chrome": True,
        "chrome_device": device,
        "standalone": standalone,
    })


def find_launch(directory: str, agent: str, launch_id: str) -> dict | None:
    """Any valid record (pending or bound) of this launch, or None."""
    _check_names(agent, launch_id)
    pending = pending_path(directory, agent, launch_id)
    candidates = [pending] + sorted(
        glob.glob(os.path.join(directory, f"{agent}.claude-launch.*.json")))
    for path in candidates:
        try:
            candidate = read_record(path, agent)
        except RecordError:
            continue
        if candidate is not None and candidate["launch_id"] == launch_id:
            return candidate
    return None


def transcript_launch_id(path: str, limit: int = 1 << 20) -> str:
    """The launch-id marker in a transcript's first ``limit`` bytes, or ""."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""
    match = MARKER_RE.search(head)
    return match.group(1) if match else ""


def claim(directory: str, agent: str, session_id: str, launch_id: str) -> dict | None:
    """Bind this session to its launch; return the record or None.

    An existing record for the session wins (resume, compaction). Otherwise
    the session is bound to the record of the launch whose id is in its own
    environment: the pending record on first start, or an already bound
    record of the same launch after /clear."""
    state = session_record(directory, agent, session_id)
    if state is not None:
        return state
    if not launch_id:
        return None
    source = find_launch(directory, agent, launch_id)
    if source is None:
        raise RecordError("no launch record for this session's launch id")
    state = {**source, "session_id": session_id}
    _write(session_path(directory, agent, session_id), state)
    try:
        os.unlink(pending_path(directory, agent, launch_id))
    except OSError:
        pass
    return state


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    command, args = argv[1], argv[2:]
    try:
        if command == "prepare" and len(args) == 5:
            prepare(args[0], args[1], args[2], args[3], args[4] == "1")
            return 0
        if command == "prompt" and len(args) == 3:
            if not valid_device(args[0]):
                raise RecordError("invalid deviceId")
            if not TOKEN_RE.fullmatch(args[2]):
                raise RecordError("invalid launch id")
            sys.stdout.write(policy_text(args[0], args[1] == "1", args[2]))
            return 0
        if command == "session" and len(args) == 4:
            # A session-start hook must never fail the session; a bad record
            # is reported to the model as "do not use the browser" instead.
            directory, agent, session_id, launch_id = args
            try:
                if not session_id:
                    if not launch_id:
                        return 0
                    raise RecordError("the session id is unknown")
                state = claim(directory, agent, session_id, launch_id)
            except (RecordError, OSError) as exc:
                # Includes a failed write while binding: the model must still
                # hear "no browser", not silence.
                reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
                print(
                    "Browser: this session's Claude in Chrome launch record is "
                    f"invalid or could not be saved ({reason}), so the target "
                    "browser is unknown. Use no browser tool, and report this to "
                    "your parent agent or the operator."
                )
                return 0
            if state is not None:
                print(policy_text(state["chrome_device"], state["standalone"],
                                  state["launch_id"]))
            return 0
    except (OSError, RecordError) as exc:
        print(f"claude_chrome_policy: {command}: {exc}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
