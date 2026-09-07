"""Current-user-only Windows state; no POSIX chmod approximation."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import lru_cache
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


def powershell(script: str, **env: str) -> str:
    child_env = {**os.environ, **env}
    # A caller running pwsh 7 can export an incompatible PSModulePath to 5.1.
    child_env = {key: value for key, value in child_env.items() if key.lower() != 'psmodulepath'}
    result = subprocess.run(
        [str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'),
         '-NoLogo', '-NoProfile', '-NonInteractive', '-Command',
         '$ErrorActionPreference="Stop"; ' + script],
        env=child_env, capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=15, check=True)
    return result.stdout.strip()


@lru_cache(maxsize=1)
def current_sid() -> str:
    if sys.platform != 'win32':
        raise RuntimeError('This helper requires native Windows')
    return powershell('[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value')


def reject_reparse(path: Path) -> None:
    for entry in (path, *path.parents):
        if entry.exists() and entry.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('Reparse points are not allowed in private state paths')


def require_private(path: Path) -> None:
    reject_reparse(path)
    data = json.loads(powershell(
        '$a=Get-Acl -LiteralPath $env:ORRERY_ACL_PATH; '
        '$rules=@($a.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]) '
        '| ForEach-Object { @{sid=$_.IdentityReference.Value;type=[int]$_.AccessControlType;'
        'rights=[int]$_.FileSystemRights} }); '
        '@{owner=$a.GetOwner([System.Security.Principal.SecurityIdentifier]).Value;'
        'rules=$rules}|ConvertTo-Json -Compress -Depth 4', ORRERY_ACL_PATH=str(path)))
    sid = current_sid()
    rules = data.get('rules') or []
    # ConvertTo-Json emits a single object rather than an array when a
    # directory happens to have exactly one access rule.
    if isinstance(rules, dict):
        rules = [rules]
    if (data['owner'] != sid or not rules
            or any(r['sid'] != sid or r['type'] != 0 for r in rules)
            or not any(r['rights'] & 0x1F01FF == 0x1F01FF for r in rules)):
        raise PermissionError('Private state must allow only the current user, with full control')


def create_private_directory(path: Path) -> None:
    """Create atomically with a protected DACL, before any secret is written."""
    path = path.absolute()
    reject_reparse(path)
    if path.exists():
        if not path.is_dir():
            raise ValueError('Private state path must be a directory')
        require_private(path)
        return
    if not path.parent.is_dir():
        raise ValueError('Create the parent directory before requesting private state')
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    descriptor = ctypes.c_void_p()
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    sid = current_sid()
    if not convert(f'O:{sid}D:P(A;OICI;FA;;;{sid})', 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('descriptor', ctypes.c_void_p),
                    ('inherit', wintypes.BOOL)]

    attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
    kernel.CreateDirectoryW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    try:
        if not kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)
    require_private(path)


def consume_token(source: Path, destination: Path) -> None:
    reject_reparse(source)
    reject_reparse(destination)
    require_private(source)
    require_private(destination.parent)
    if not source.is_file() or source.stat().st_size > 4096:
        raise ValueError('Token handoff must be a regular file of at most 4096 bytes')
    token = source.read_text(encoding='utf-8').strip()
    if not token or '\n' in token or '\r' in token:
        raise ValueError('Token handoff must contain one non-empty token')
    with destination.open('x', encoding='utf-8') as stream:
        stream.write(token)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        require_private(destination)
    except BaseException:
        # Never leave a newly written token behind when its ACL cannot be
        # verified. The source remains intact so the caller can recover.
        destination.unlink(missing_ok=True)
        raise
    source.unlink()
