"""Read-only readiness check for Chrome DevTools MCP's default profile."""

from pathlib import Path
import logging
import os
import socket
import subprocess
import time

LOG = logging.getLogger(__name__)


_WINDOWS_CHROME_CHECK = r"""
$ErrorActionPreference = 'Stop'
$running = Get-Process -Name chrome -ErrorAction SilentlyContinue
Write-ProbePhase 'chromeprocess'
if (-not $running) {
    'stopped'
    return
}
$marker = Join-Path $env:LOCALAPPDATA 'Google\Chrome\User Data\DevToolsActivePort'
if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
    Write-ProbePhase 'chromeport'
    'debugging'
    return
}
$line = Get-Content -LiteralPath $marker -TotalCount 1
Write-ProbePhase 'chromeport'
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
Write-ProbePhase 'chrometcp'
"""


_WINDOWS_PROBE_HEADER = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$entry = [DateTime]::UtcNow
$clock = [Diagnostics.Stopwatch]::StartNew()
function Write-ProbePhase($name, $value = '') {
    [Console]::WriteLine("ORRERY_PROBE|$name|$($clock.ElapsedMilliseconds)|$value")
    [Console]::Out.Flush()
}
$startupMs = ''
try {
    $process = [Diagnostics.Process]::GetCurrentProcess()
    $startupMs = [int][Math]::Max(0, ($entry - $process.StartTime.ToUniversalTime()).TotalMilliseconds)
} catch {}
Write-ProbePhase 'started' $startupMs
"""

_CHROME_STATES = {"stopped", "debugging", "ready"}


def windows_catalog_state(*, profile: bool, chrome: bool):
    """Return both catalog hints and the chosen interop for the Cua Driver check."""
    # Native Windows imports this module for portable startup.
    if __package__:
        from . import wsl_anchor, wsl_catalog_probe
    else:
        import wsl_anchor
        import wsl_catalog_probe

    started = time.monotonic()
    try:
        env = wsl_anchor.interop_env()
    except OSError:
        wsl_catalog_probe.LOG.warning("WSL catalog probe interop_socket_lookup_failed elapsed_ms=%d",
                                      round((time.monotonic() - started) * 1000))
        return None, "interop", os.environ.get("WSL_INTEROP", "")
    path = env.get("WSL_INTEROP", "")
    socket_state = ("present" if os.path.exists(path) else "missing") if path else "unset"
    wsl_catalog_probe.LOG.info("WSL catalog probe interop_select_ms=%d interop_socket=%s",
                               round((time.monotonic() - started) * 1000), socket_state)
    if not (profile or chrome):
        return None, "ready", path

    lookup_started = time.monotonic()
    powershell = wsl_anchor._powershell()
    wsl_catalog_probe.LOG.info("WSL catalog probe powershell_lookup_ms=%d",
                               round((time.monotonic() - lookup_started) * 1000))
    if not powershell:
        wsl_catalog_probe.LOG.warning("WSL catalog probe powershell_not_found")
        return None, "interop", path
    script = _WINDOWS_PROBE_HEADER
    if profile:
        script += "\nWrite-ProbePhase 'profile' $env:USERPROFILE\n"
    if chrome:
        script += "$chrome = & {\n" + _WINDOWS_CHROME_CHECK + "}\nWrite-ProbePhase 'chrome' $chrome\n"
    script += "Write-ProbePhase 'finished'\n"
    status, _, phases = wsl_catalog_probe.run_phased_probe(
        "powershell", [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        env=env, timeout=3 if chrome else 2)

    profile_phase = phases.get("profile")
    profile_path = None
    if profile_phase and profile_phase.arrival_ms <= 2000:
        path_started = time.monotonic()
        profile_path = wsl_anchor.wsl_profile_path(profile_phase.value)
        wsl_catalog_probe.LOG.info("WSL catalog probe wslpath_ms=%d profile_resolved=%s",
                                   round((time.monotonic() - path_started) * 1000),
                                   profile_path is not None)
    if not chrome:
        return profile_path, "ready", path
    if status != "ok":
        # Preserve the observable distinction between Windows interop and a
        # completed check that confirms Chrome is unavailable.
        LOG.warning("Windows Chrome check failed: %s (interop_socket=%s)", status, socket_state)
        return profile_path, "interop", path
    chrome_phase = phases.get("chrome")
    result = chrome_phase.value if chrome_phase and chrome_phase.value in _CHROME_STATES else "unknown"
    return profile_path, result, path


def _windows_state():
    return windows_catalog_state(profile=False, chrome=True)[1]


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


def personal_browser_reason(*, wsl, state=None):
    if state is None:
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
