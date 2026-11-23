"""Inspect ownership and readiness of an installed systemd dashboard."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


_BUS = "org.freedesktop.systemd1"


def _query(*args):
    return subprocess.run(args, capture_output=True, text=True, timeout=5)


def dashboard_main_pid(unit: Path, runner: Path, interpreters: list[str]) -> int | None:
    """Return the owned unit's MainPID, or None if the label is unused."""
    result = _query("systemctl", "--user", "show", "--all", "--property=LoadState,FragmentPath,MainPID", "--", unit.name)
    props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    state = props.get("LoadState")
    fragment = props.get("FragmentPath")
    if state == "not-found" and fragment == "" and not os.path.lexists(unit):
        return None
    if (result.returncode or state != "loaded" or fragment != str(unit)
            or unit.is_symlink() or not unit.is_file()):
        raise ValueError(f"cannot verify dashboard unit {unit.name} at {unit} (fragment: {fragment or 'unknown'})")

    # Read ExecStart as structured argv: systemctl show joins arguments for
    # display, which is ambiguous for paths containing spaces.
    result = _query("busctl", "--user", "--json=short", "call",
                    _BUS, "/org/freedesktop/systemd1", _BUS + ".Manager", "LoadUnit", "s", unit.name)
    result.check_returncode()
    (obj,) = json.loads(result.stdout)["data"]
    result = _query("busctl", "--user", "--json=short", "get-property",
                    _BUS, obj, _BUS + ".Service", "ExecStart")
    result.check_returncode()
    commands = json.loads(result.stdout)["data"]
    if len(commands) == 1:
        executable, argv, *_ = commands[0]
        trusted = {Path(path).resolve() for path in interpreters if path}
        if (len(argv) == 2 and argv[0] == executable and Path(executable).resolve() in trusted
                and Path(argv[1]).is_absolute() and Path(argv[1]).resolve() == runner.resolve()):
            return int(props["MainPID"])
    raise ValueError(f"dashboard unit {unit.name} does not run {runner}")


def wait_dashboard_ready(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/api/agents"
    deadline = time.monotonic() + 15
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return True
                last_error = f"HTTP {response.status}"
        except OSError as exc:
            last_error = str(exc)
        time.sleep(0.25)
    print(f"dashboard health check failed: {last_error}", file=sys.stderr)
    return False


def main() -> int:
    try:
        if sys.argv[1] == "ready":
            return 0 if wait_dashboard_ready(int(sys.argv[2])) else 1
        pid = dashboard_main_pid(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4:])
        if pid is None:
            return 3
        print(pid)
        return 0
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as exc:
        print(f"dashboard systemd check failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
