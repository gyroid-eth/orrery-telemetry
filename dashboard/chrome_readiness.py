"""Read-only readiness check for Chrome DevTools MCP's default profile."""

from pathlib import Path
import logging
import os
import socket
import subprocess

LOG = logging.getLogger(__name__)


_WINDOWS_CHECK = r"""
$ErrorActionPreference = 'Stop'
if (-not (Get-Process -Name chrome -ErrorAction SilentlyContinue)) {
    'stopped'
    return
}
$marker = Join-Path $env:LOCALAPPDATA 'Google\Chrome\User Data\DevToolsActivePort'
if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
    'debugging'
    return
}
$line = Get-Content -LiteralPath $marker -TotalCount 1
[int]$port = 0
if (-not [int]::TryParse($line, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
    'debugging'
    return
}
$client = [Net.Sockets.TcpClient]::new()
try {
    $connection = $client.ConnectAsync('127.0.0.1', $port)
    try {
        if ($connection.Wait(250) -and $client.Connected) { 'ready' } else { 'debugging' }
    } catch { 'debugging' }
} finally { $client.Dispose() }
"""


def _windows_failure(reason, env=None):
    socket_state = "unknown"
    if env is not None:
        path = env.get("WSL_INTEROP")
        socket_state = ("present" if os.path.exists(path) else "missing") if path else "unset"
    LOG.warning("Windows Chrome check failed: %s (interop_socket=%s)", reason, socket_state)
    return "interop"


def _windows_state():
    # Only the WSL probe needs the POSIX anchor and its file locks.
    if __package__:
        from . import wsl_anchor
    else:
        import wsl_anchor

    powershell = wsl_anchor._powershell()
    if not powershell:
        return _windows_failure("powershell_not_found")
    previous = None
    for _ in range(2):
        try:
            env = wsl_anchor.interop_env()
        except wsl_anchor.InteropUnavailable:
            return _windows_failure("no_usable_interop_socket")
        except OSError:
            return _windows_failure("interop_socket_lookup_failed")
        path = env.get("WSL_INTEROP", "")
        if path == previous:
            break
        state = _windows_chrome_state(powershell, env)
        if state != "interop":
            return state
        wsl_anchor.invalidate_interop(path)
        previous = path
    return "interop"


def _windows_chrome_state(powershell, env):
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_CHECK],
            env=env, capture_output=True, text=True, timeout=3, check=True,
        )
    except subprocess.TimeoutExpired:
        return _windows_failure("powershell_timeout", env)
    except subprocess.CalledProcessError as error:
        return _windows_failure(f"powershell_nonzero_exit={error.returncode}", env)
    except (OSError, subprocess.SubprocessError, ValueError):
        return _windows_failure("powershell_start_failed", env)
    answer = result.stdout.strip()
    return answer if answer in {"stopped", "debugging", "ready"} else "unknown"


def _mac_state():
    try:
        chrome = subprocess.run(
            ["/usr/bin/pgrep", "-x", "Google Chrome"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if chrome.returncode == 1:
        return "stopped"
    if chrome.returncode != 0:
        return "unknown"
    marker = Path.home() / "Library/Application Support/Google/Chrome/DevToolsActivePort"
    try:
        with marker.open("rb") as source:
            raw_port = source.readline(16).strip()
    except FileNotFoundError:
        return "debugging"
    except PermissionError:
        return "permission"
    except OSError:
        return "unknown"
    if not raw_port.isdigit() or not 1 <= (port := int(raw_port)) <= 65535:
        return "debugging"
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return "ready"
    except OSError:
        return "debugging"


def personal_browser_reason(*, wsl):
    state = _windows_state() if wsl else _mac_state()
    host = "Windows" if wsl else "this Mac"
    setup = "Open chrome://inspect/#remote-debugging in Chrome and enable remote debugging."
    return {
        "stopped": f"Chrome is not running on {host}. Start Google Chrome, then enable remote debugging in chrome://inspect/#remote-debugging.",
        "debugging": f"Chrome's remote debugging is unavailable on {host}. {setup}",
        "permission": "ORRERY cannot read Chrome's debugging status. Allow access to Chrome's data in macOS Privacy & Security.",
        "interop": f"ORRERY cannot check Windows Chrome from WSL. Check Windows interop. {setup}",
        "unknown": f"ORRERY could not check Chrome on {host}. {setup}",
    }.get(state, ""), state in {"stopped", "debugging"}
