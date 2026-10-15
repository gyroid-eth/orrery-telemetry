"""Operator-root-only local model profiles; no ambient HOME lookup or effects."""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from local_model_auth import LocalModelError, canonical_digest, plain_text, secure_read

INVALID = "LOCAL_MODEL_PROFILE_INVALID"
UNAVAILABLE = "LOCAL_MODEL_PROFILE_UNAVAILABLE"
NAME = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z", re.ASCII)
COMMON = {"schema_version", "name", "model", "context_window", "max_output_tokens"}
ADAPTERS = {"codex": "responses", "claude": "anthropic-messages"}


def selector_name(ref):
    if not isinstance(ref, str) or not ref.startswith("local:") or not NAME.fullmatch(ref[6:]):
        raise LocalModelError(INVALID)
    return ref[6:]


def validate_url(value, *, authenticated=False):
    if not plain_text(value) or any(char.isspace() for char in value) or "\\" in value:
        raise LocalModelError(INVALID)
    try:
        url = urlsplit(value)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None or
                url.password is not None or "?" in value or "#" in value or
                (url.port is not None and not 1 <= url.port <= 65535)):
            raise ValueError("invalid endpoint")
        if url.scheme == "http":
            try:
                loopback = ipaddress.ip_address(url.hostname).is_loopback
            except ValueError:
                loopback = url.hostname == "localhost"
            if not loopback or authenticated:
                raise ValueError("TLS required")
    except ValueError:
        raise LocalModelError(INVALID) from None


def validate_profile(value, name):
    if (not isinstance(value, dict) or set(value) - COMMON - ADAPTERS.keys() or
            not COMMON <= value.keys() or not ADAPTERS.keys() & value.keys() or
            type(value["schema_version"]) is not int or value["schema_version"] != 1 or
            value["name"] != name or not NAME.fullmatch(name) or not plain_text(value["model"]) or
            not value["model"].strip()):
        raise LocalModelError(INVALID)
    window, output = value["context_window"], value["max_output_tokens"]
    if type(window) is not int or type(output) is not int or not 0 < output < window <= 2**63 - 1:
        raise LocalModelError(INVALID)
    for provider, protocol in ADAPTERS.items():
        if provider not in value:
            continue
        adapter = value[provider]
        if (not isinstance(adapter, dict) or set(adapter) - {"base_url", "wire_api", "auth_command", "env"} or
                not {"base_url", "wire_api"} <= adapter.keys() or adapter["wire_api"] != protocol):
            raise LocalModelError(INVALID)
        command = adapter.get("auth_command")
        if command is not None and (not isinstance(command, list) or not command or
                                    not all(plain_text(arg) for arg in command) or
                                    not Path(command[0]).is_absolute()):
            raise LocalModelError(INVALID)
        validate_url(adapter["base_url"], authenticated=command is not None)
        env = adapter.get("env", {})
        if not isinstance(env, dict) or set(env) - ({"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"} if provider == "claude" else set()):
            raise LocalModelError(INVALID)
        for setting in env.values():
            if not isinstance(setting, str) or not re.fullmatch(r"[0-9]{1,3}", setting) or not 1 <= int(setting) <= 100:
                raise LocalModelError(INVALID)
    return value


def load_profile(root, ref, *, expected_uid=None):
    name = selector_name(ref)
    try:
        if expected_uid is not None and (type(expected_uid) is not int or expected_uid < 0):
            raise ValueError("invalid operator UID")
        raw = secure_read(Path(root) / (name + ".toml"),
                          os.getuid() if expected_uid is None else expected_uid,
                          mode=0o600, parent_mode=0o700, limit=65536)
        return validate_profile(tomllib.loads(raw.decode("utf-8")), name)
    except FileNotFoundError:
        raise LocalModelError(UNAVAILABLE) from None
    except (OSError, ValueError, TypeError):
        raise LocalModelError(INVALID) from None


def profile_digest(profile):
    return canonical_digest(profile)
