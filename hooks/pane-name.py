"""Maintain legacy hook pane names for one observed tmux server."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


def observe_server(pane):
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane,
             "#{pid}|#{start_time}|#{pane_id}|#{socket_path}"],
            capture_output=True, text=True, check=True, timeout=2,
        )
        pid, started, actual_pane, socket = result.stdout.rstrip("\n").split("|", 3)
        if (not pid.isdecimal() or not started.isdecimal() or int(pid) < 1 or int(started) < 1
                or actual_pane != pane or not Path(socket).is_absolute()):
            return None
        return os.path.realpath(socket), int(pid), int(started)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def read_record(path):
    try:
        with open(path, "rb", opener=lambda name, flags: os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK)) as record:
            info = os.fstat(record.fileno())
            if stat.S_ISREG(info.st_mode) and info.st_size <= 4096:
                return record.read(4097).decode("utf-8"), info
    except (FileNotFoundError, UnicodeError):
        pass
    except OSError:
        if not path.is_symlink():
            raise
    return "", None


def discard(path, info):
    # The previous release did not take the lock for unscoped files.
    try:
        current = path.lstat()
        if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_ctime_ns, current.st_size) == (
                info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size):
            path.unlink()
    except FileNotFoundError:
        pass


def write_record(path, record):
    path.parent.parent.mkdir(mode=0o700, exist_ok=True)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        staging = Path(temporary.name)
        try:
            json.dump(record, temporary)
            temporary.close()
            os.replace(staging, path)
        finally:
            staging.unlink(missing_ok=True)


def update(action, runtime, pane, name):
    if not re.fullmatch(r"%[0-9]+", pane):
        return
    if action == "write":
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not runtime.is_dir():
        return
    key = "agent_name_" + pane.replace("%", "_")
    legacy = runtime / key
    generations = runtime / "pane-names"
    if action != "write" and not legacy.exists() and not generations.is_dir():
        return

    # Serialize the observation and file update so a late call cannot prune a
    # name published by a newer call. Each socket has its own pane namespace.
    with open(runtime / ".pane-names.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        server = observe_server(pane)
        inherited = os.environ.get("TMUX", "").rsplit(",", 2)
        if server and len(inherited) == 3:
            expected_socket, expected_pid, _ = inherited
            if (str(server[1]) != expected_pid or server[0] != os.path.realpath(expected_socket)):
                # A hook from an old server may still be finishing after a restart.
                return
        current = ""
        if server:
            socket, pid, started = server
            record = generations / hashlib.sha256(os.fsencode(socket)).hexdigest() / key
            raw, info = read_record(record)
            if info:
                try:
                    saved = json.loads(raw)
                    if saved["server"] == [pid, started] and isinstance(saved["name"], str):
                        current = saved["name"]
                    else:
                        discard(record, info)
                except (KeyError, TypeError, ValueError):
                    discard(record, info)
            if action == "write":
                write_record(record, {"server": [pid, started], "name": name})
            elif action == "remove" and current == name and info:
                discard(record, info)

        unscoped, info = read_record(legacy)
        if info and server and info.st_mtime_ns < server[2] * 1_000_000_000:
            discard(legacy, info)
            unscoped = ""
        elif info and action == "remove" and "".join(unscoped.split()) == name:
            discard(legacy, info)
        if action == "read" and current:
            print("verified|" + current)
        elif action == "read" and unscoped:
            print("legacy|" + "".join(unscoped.split()))


if __name__ == "__main__":
    operation, directory, target, *extra = sys.argv[1:]
    if operation not in ("read", "write", "remove"):
        raise SystemExit(2)
    update(operation, Path(directory), target, extra[0] if extra else "")
