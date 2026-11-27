"""Resolve a personal Chromium browser on the machine running DevTools MCP."""

from __future__ import annotations

import http.client
import json
import logging
import os
from pathlib import Path, PureWindowsPath
import re
import runpy
import struct
import subprocess
import sys
from typing import NamedTuple

LOG = logging.getLogger(__name__)
BROWSERS = {
    "chrome": ("Google Chrome", "com.google.Chrome", "Google/Chrome", "chrome"),
    "brave": ("Brave", "com.brave.Browser", "BraveSoftware/Brave-Browser", "brave"),
    "edge": ("Microsoft Edge", "com.microsoft.edgemac", "Microsoft Edge", "edge"),
}
_BROWSER_PATH = re.compile(r"/devtools/browser/[0-9A-Za-z_-]{1,100}\Z")
_MAC_EXECUTABLES = {"Google Chrome": "chrome", "Brave Browser": "brave", "Microsoft Edge": "edge"}
_DEFAULT_MAC_APP = """
ObjC.import('AppKit');
const url = $.NSURL.URLWithString('https://example.invalid/');
const app = $.NSWorkspace.sharedWorkspace.URLForApplicationToOpenURL(url);
ObjC.unwrap($.NSBundle.bundleWithURL(app).bundleIdentifier);
"""

# Inventory and loopback checks run on Windows, including with WSL NAT.
_WINDOWS_PROBE = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$profiles = @{
    'chrome.exe' = @{ id = 'chrome'; path = 'Google\Chrome\User Data' }
    'brave.exe' = @{ id = 'brave'; path = 'BraveSoftware\Brave-Browser\User Data' }
    'msedge.exe' = @{ id = 'edge'; path = 'Microsoft\Edge\User Data' }
}
function SwitchValue($line, $name) {
    $switch = '--' + [regex]::Escape($name)
    $pattern = '(?<!\S)(?:"' + $switch + '=([^"]+)"|' + $switch + '(?:=|\s+)(?:"([^"]+)"|([^\s"]+)))(?=\s|$)'
    $result = [regex]::Matches($line, $pattern)
    if ($result.Count) {
        foreach ($group in $result[$result.Count - 1].Groups[1..3]) {
            if ($group.Success) { return $group.Value }
        }
    }
}
function RemoteMarker($dir) {
    $marker = Join-Path $dir 'DevToolsActivePort'
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) { return }
    if ((Get-Item -LiteralPath $marker).Length -gt 1024) { return }
    $lines = @(Get-Content -LiteralPath $marker -TotalCount 2)
    if ($lines.Count -eq 2 -and $lines[0] -match '^\d{1,5}$' -and
        $lines[1] -cmatch '^/devtools/browser/[0-9A-Za-z_-]{1,100}$') { return [int]$lines[0] }
}
function CheckFixedPort($port) {
    $response = $null
    $reader = $null
    try {
        $request = [Net.HttpWebRequest]::Create("http://127.0.0.1:$port/json/version")
        $request.Proxy = $null
        $request.AllowAutoRedirect = $false
        $request.Timeout = 350
        $request.ReadWriteTimeout = 350
        $response = $request.GetResponse()
        if ([int]$response.StatusCode -ne 200) { return $false }
        $reader = [IO.StreamReader]::new($response.GetResponseStream())
        $buffer = [char[]]::new(8193)
        $count = $reader.ReadBlock($buffer, 0, $buffer.Length)
        if ($count -le 0 -or $count -ge $buffer.Length) { return $false }
        $data = (-join $buffer[0..($count - 1)]) | ConvertFrom-Json
        return $data.webSocketDebuggerUrl -cmatch ('^ws://127\.0\.0\.1:' + $port + '/devtools/browser/[0-9A-Za-z_-]{1,100}$')
    } catch { return $false }
    finally {
        if ($reader) { $reader.Dispose() }
        if ($response) { $response.Dispose() }
    }
}
$default = Get-ItemPropertyValue -LiteralPath 'HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice' -Name ProgId -ErrorAction SilentlyContinue
$processes = @(Get-CimInstance Win32_Process -Filter "Name='chrome.exe' OR Name='brave.exe' OR Name='msedge.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -notmatch '(?<!\S)"?--(?:type|headless|enable-automation)(?:[=\s"]|$)' } |
    Sort-Object ProcessId)
$listeners = @()
if ($processes.Count) {
    try {
        $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
            Where-Object { $_.LocalAddress -eq '127.0.0.1' })
    } catch {
        if ($_.CategoryInfo.Category -ne 'ObjectNotFound') { throw }
    }
}
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$candidates = @(foreach ($process in $processes) {
    $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwnerSid
    if ($owner.ReturnValue -ne 0 -or $owner.Sid -ne $sid) { continue }
    $browser = $profiles[$process.Name]
    $dir = SwitchValue $process.CommandLine 'user-data-dir'
    $standard = Join-Path $env:LOCALAPPDATA $browser.path
    if (-not $dir) { $dir = $standard }
    $row = @{ browser = $browser.id; status = 'debugging'; standard = $dir -eq $standard }
    $fixed = SwitchValue $process.CommandLine 'remote-debugging-port'
    $customUserData = $dir -match '^[A-Za-z]:\\[^\x00-\x1f]+$'
    $port = if ($fixed -match '^[1-9]\d{0,4}$') { [int]$fixed } elseif ($customUserData) { RemoteMarker $dir }
    if ($port -gt 0 -and $port -le 65535 -and ($listeners | Where-Object {
        $_.OwningProcess -eq $process.ProcessId -and $_.LocalPort -eq $port
    })) {
        if ($fixed -match '^[1-9]\d{0,4}$') {
            if (CheckFixedPort $port) { $row.status = 'ready'; $row.port = $port }
        } elseif ($customUserData) {
            $row.status = 'ready'
            $row.directory = $dir
        }
    }
    $row
})
ConvertTo-Json -InputObject @{ default = $default; candidates = $candidates } -Depth 4 -Compress
"""


class Result(NamedTuple):
    browser: str | None
    state: str
    args: tuple[str, ...] = ()

    @property
    def label(self) -> str | None:
        return BROWSERS[self.browser][0] if self.browser else None

    def reason(self, wsl: bool) -> str:
        host = "Windows" if wsl else "this Mac"
        if self.state == "ready":
            return ""
        if self.state == "interop":
            return "ORRERY cannot check Windows browsers from WSL. Check Windows interop."
        if self.state == "stopped":
            return f"Start Chrome, Brave, or Edge on {host} and enable remote debugging."
        if self.browser:
            label, _, _, scheme = BROWSERS[self.browser]
            return (f"Open {scheme}://inspect/#remote-debugging in {label} and enable remote debugging."
                    if self.state == "debugging"
                    else f"ORRERY could not check {label}'s remote debugging on {host}.")
        return f"ORRERY could not check Chrome, Brave, or Edge on {host}."


def _windows_default(prog_id):
    if not isinstance(prog_id, str):
        return None
    return next((key for key, prefix in (("chrome", "chromehtml"), ("brave", "bravehtml"),
                                          ("edge", "msedgehtm"))
                 if prog_id.lower().startswith(prefix)), None)


def _choose(candidates, default=None, *, windows=False):
    def order(row):
        return (row["browser"] != default, list(BROWSERS).index(row["browser"]), not row.get("standard"))

    states = []
    for row in sorted((item for item in candidates if isinstance(item, dict) and
                       isinstance(item.get("browser"), str) and item["browser"] in BROWSERS), key=order):
        browser, state = row["browser"], row.get("status")
        if state == "ready":
            port, directory = row.get("port"), row.get("directory")
            if type(port) is int and 1 <= port <= 65535:
                return Result(browser, state, ("--browserUrl", f"http://127.0.0.1:{port}"))
            profile = PureWindowsPath(directory) if windows and isinstance(directory, str) else directory
            absolute = (isinstance(profile, PureWindowsPath) and profile.is_absolute() if windows
                        else isinstance(profile, str) and os.path.isabs(profile))
            if absolute and isinstance(directory, str) and not re.search(r"[\x00-\x1f]", directory):
                return Result(browser, state, ("--autoConnect", "--userDataDir", directory))
        states.append(Result(browser, "debugging" if state == "debugging" else "unknown"))
    if states:
        return next((result for result in states if result.state == "unknown"), states[0])
    return Result(None, "stopped")


def _windows_result():
    # Load lazily so macOS and the portable Windows dashboard can import this module.
    anchor = runpy.run_path(str(Path(__file__).resolve().parents[1] / "dashboard/wsl_anchor.py"))
    powershell = anchor["_powershell"]()
    env = None
    failure = "powershell_not_found"
    if powershell:
        try:
            env = anchor["interop_env"]()
            output = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_PROBE],
                env=env, encoding="utf-8", capture_output=True, check=True, timeout=5,
            ).stdout
            snapshot = json.loads(output)
            if isinstance(snapshot, dict) and isinstance(snapshot.get("candidates"), list):
                return _choose(snapshot["candidates"], _windows_default(snapshot.get("default")), windows=True)
            return Result(None, "unknown")
        except subprocess.TimeoutExpired:
            failure = "powershell_timeout"
        except subprocess.CalledProcessError as error:
            failure = f"powershell_nonzero_exit={error.returncode}"
        except (UnicodeError, ValueError):
            return Result(None, "unknown")
        except OSError:
            failure = "interop_socket_lookup_failed" if env is None else "powershell_start_failed"
    path = (env or {}).get("WSL_INTEROP", "")
    socket_state = "unknown" if env is None else "unset"
    if path:
        socket_state = "present" if os.path.exists(path) else "missing"
    LOG.warning("Windows browser check failed: %s (interop_socket=%s)", failure, socket_state)
    return Result(None, "interop")


def _mac_processes():
    output = subprocess.run(
        ["/bin/ps", "-ww", "-U", str(os.getuid()), "-o", "pid=,comm="],
        capture_output=True, text=True, timeout=2, check=True,
    ).stdout
    for line in output.splitlines():
        fields = line.split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit() or not fields[1].startswith("/"):
            continue
        pid, executable_path = int(fields[0]), fields[1]
        for executable, browser in _MAC_EXECUTABLES.items():
            if not executable_path.endswith(f"/Contents/MacOS/{executable}"):
                continue
            try:
                args = _mac_argv(pid)
            except ProcessLookupError:
                break
            except OSError:
                args = None
            if args is None or not any(arg.split("=", 1)[0] in {"--type", "--headless", "--enable-automation"}
                                       for arg in args):
                yield pid, browser, args
            break


def _mac_argv(pid):
    """Read the kernel's argv boundaries for paths containing spaces."""
    import ctypes

    # <sys/sysctl.h>: CTL_KERN, KERN_PROCARGS2, pid. The buffer begins with
    # argc, the executable's path, zero padding, and then the NUL-ended argv.
    query = (ctypes.c_int * 3)(1, 49, pid)
    sysctl = ctypes.CDLL(None, use_errno=True).sysctl
    sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                       ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    sysctl.restype = ctypes.c_int
    size = ctypes.c_size_t()
    if sysctl(query, len(query), None, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), "could not size browser argv")
    if size.value < 5 or size.value > 8 * 1024 * 1024:
        raise OSError("invalid browser argv size")
    result = ctypes.create_string_buffer(size.value)
    if sysctl(query, len(query), result, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), "could not read browser argv")
    data = result.raw[:size.value]
    if len(data) < 5:
        raise OSError("invalid browser argv size")
    count = struct.unpack_from("=i", data)[0]
    if not 1 <= count <= 4096:
        raise OSError("invalid browser argv count")
    argv = data[4:].partition(b"\0")[2].lstrip(b"\0").split(b"\0", count)
    if len(argv) <= count:
        raise OSError("truncated browser argv")
    return [os.fsdecode(item) for item in argv[1:count]]


def _mac_default():
    try:
        bid = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", _DEFAULT_MAC_APP],
            capture_output=True, text=True, timeout=1, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return next((browser for browser, spec in BROWSERS.items() if spec[1] == bid), None)


def _mac_switch(args, name):
    key = "--" + name
    value = ""
    for index, arg in enumerate(args):
        if arg.startswith(key + "="):
            value = arg.partition("=")[2]
        elif arg == key and index + 1 < len(args) and not args[index + 1].startswith("--"):
            value = args[index + 1]
    return value


def _marker_port(directory):
    try:
        with (directory / "DevToolsActivePort").open("rb") as source:
            lines = source.read(1025).decode("ascii").splitlines()
    except (FileNotFoundError, UnicodeError, IsADirectoryError):
        return None
    if (len(lines) == 2 and 1 <= len(lines[0]) <= 5 and lines[0].isdigit()
            and _BROWSER_PATH.fullmatch(lines[1])):
        port = int(lines[0])
        if 1 <= port <= 65535:
            return port
    return None


def _mac_owns_port(pid, port):
    answer = subprocess.run(
        ["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), f"-iTCP:{port}",
         "-sTCP:LISTEN", "-Fn"],
        capture_output=True, text=True, timeout=1,
    )
    if answer.returncode not in (0, 1) or answer.stderr:
        raise OSError("could not check the browser listener owner")
    return f"n127.0.0.1:{port}" in answer.stdout.splitlines()


def _is_fixed_cdp_port(port):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.35)
    try:
        conn.request("GET", "/json/version")
        response = conn.getresponse()
        endpoint = json.loads(response.read(8193)).get("webSocketDebuggerUrl") if response.status == 200 else None
        return isinstance(endpoint, str) and re.fullmatch(
            rf"ws://127\.0\.0\.1:{port}/devtools/browser/[0-9A-Za-z_-]{{1,100}}", endpoint
        ) is not None
    except (OSError, ValueError, AttributeError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def _mac_result():
    try:
        processes = list(_mac_processes())
    except (OSError, subprocess.SubprocessError):
        return Result(None, "unknown")
    candidates = []
    for pid, browser, args in processes:
        standard = Path.home() / "Library/Application Support" / BROWSERS[browser][2]
        if args is None:
            candidates.append({"browser": browser, "status": "unknown"})
            continue
        directory = Path(_mac_switch(args, "user-data-dir") or standard)
        row = {"browser": browser, "status": "debugging", "standard": directory == standard}
        fixed = _mac_switch(args, "remote-debugging-port")
        fixed_port = int(fixed) if re.fullmatch(r"[1-9][0-9]{0,4}", fixed) else None
        try:
            port = fixed_port
            if port is None and directory.is_absolute():
                port = _marker_port(directory)
            if port and port <= 65535 and _mac_owns_port(pid, port):
                if fixed_port is None:
                    row.update(status="ready", directory=str(directory))
                elif _is_fixed_cdp_port(port):
                    row.update(status="ready", port=port)
        except (OSError, subprocess.SubprocessError):
            row["status"] = "unknown"
        candidates.append(row)
    return _choose(candidates, _mac_default() if len(candidates) > 1 else None)


def discover(*, wsl=False):
    if wsl:
        return _windows_result()
    if sys.platform == "darwin":
        return _mac_result()
    return Result(None, "unmanaged")
