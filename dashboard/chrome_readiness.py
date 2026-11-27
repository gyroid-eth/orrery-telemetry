"""Read-only readiness check for Chrome DevTools MCP's default profile."""

from pathlib import Path
import socket
import subprocess

try:
    from dashboard import wsl_anchor
except ModuleNotFoundError:
    import wsl_anchor


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


def _windows_state():
    powershell = wsl_anchor._powershell()
    if not powershell:
        return "interop"
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_CHECK],
            env=wsl_anchor.interop_env(), capture_output=True, text=True, timeout=3, check=True,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return "interop"
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
