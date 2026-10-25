"""Fixed HTTP authorities for the unauthenticated legacy dashboard.

Pure parsing only: no DNS, network, credential access or global-mode policy.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

SETTING = "AGENTSTACK_DASHBOARD_ALLOWED_ORIGINS"
Origin = tuple[str, str, int]


def authority(value: str) -> tuple[str, int | None]:
    if not value or not value.isascii() or re.search(r'[\s,/@\\?#%"\'<>]', value):
        raise ValueError("invalid authority")
    if value.startswith("["):
        match = re.fullmatch(r"\[([^]]+)\](?::([0-9]+))?", value)
        if not match:
            raise ValueError("invalid IPv6 authority")
        host = str(ipaddress.IPv6Address(match[1]))
        port = match[2]
    else:
        match = re.fullmatch(r"([^:]+)(?::([0-9]+))?", value)
        if not match:
            raise ValueError("invalid authority")
        host, port = match[1].lower(), match[2]
        if len(host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                  for label in host.split(".")):
            raise ValueError("invalid host")
    if port is not None and not 1 <= int(port) <= 65535:
        raise ValueError("invalid port")
    return host, int(port) if port is not None else None


def origin(value: str) -> Origin:
    match = re.fullmatch(r"(https?)://([^/]+)", value, re.IGNORECASE)
    if not match:
        raise ValueError("invalid origin")
    scheme = match[1].lower()
    host, port = authority(match[2])
    return scheme, host, port if port is not None else (443 if scheme == "https" else 80)


def configured_origins(value: str) -> frozenset[Origin]:
    # Only ASCII spaces separate entries. Reject controls before any writer
    # stores this setting in an env file, plist, unit or install receipt.
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(SETTING + " must contain space-separated HTTP(S) origins")
    try:
        return frozenset(origin(item) for item in value.split(" ") if item)
    except ValueError:
        raise ValueError(SETTING + " must contain space-separated HTTP(S) origins") from None


def admit(headers, target: str, port: int, extra: str = "") -> frozenset[Origin]:
    allowed = configured_origins(extra) | frozenset(
        ("http", host, port) for host in ("localhost", "127.0.0.1", "::1"))
    hosts = headers.get_all("Host", [])
    if len(hosts) != 1 or len(headers.get_all("Origin", [])) > 1 or headers.get_all("Authority", []):
        raise ValueError("ambiguous authority")
    host, host_port = authority(hosts[0])
    expected = frozenset(item for item in allowed if item[1] == host and item[2] == (
        host_port if host_port is not None else (443 if item[0] == "https" else 80)))
    if not expected:
        raise ValueError("untrusted authority")
    # Proxies may send absolute-form targets. Do not route a target naming a
    # different authority, even if that other authority is also configured.
    if not target.startswith("/") and target != "*":
        parts = urlsplit(target)
        target_origin = origin(parts.scheme + "://" + parts.netloc)
        if target_origin not in expected:
            raise ValueError("request target authority mismatch")
        expected = frozenset((target_origin,))
    return expected


def origin_matches(value: str, expected: frozenset[Origin]) -> bool:
    try:
        return origin(value) in expected
    except ValueError:
        return False
