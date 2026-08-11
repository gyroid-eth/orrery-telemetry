"""Write-once client configuration seal for the same-endpoint cutover.

The seal contains only file metadata and cryptographic digests.  The bearer
value is held in memory just long enough to compare the Claude and legacy
sources and to construct a verified request header; it is never serialized or
included in an exception message.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Final

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 only
    import tomli as tomllib  # type: ignore[no-redef]


EXPECTED_ENDPOINT: Final[str] = "http://127.0.0.1:8765/api/"
CLAUDE_CLIENT_KEY: Final[str] = "mcp-agent-mail"
CODEX_CLIENT_KEY: Final[str] = "agent-mail"
CODEX_BEARER_ENV: Final[str] = "MCP_AGENT_MAIL_TOKEN"
LEGACY_BEARER_KEY: Final[str] = "HTTP_BEARER_TOKEN"
SEAL_KIND: Final[str] = "orrery-mail-client-config-seal"
SEAL_VERSION: Final[int] = 1


class ClientConfigSealError(RuntimeError):
    """A client setting is missing, changed, unsafe, or unpinned."""


def _read_stable_regular(path: Path) -> tuple[bytes, os.stat_result]:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise ClientConfigSealError("client configuration is not a regular file")
        raw = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise ClientConfigSealError("client configuration is unavailable") from exc
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after or len(raw) != after.st_size:
        raise ClientConfigSealError("client configuration changed while reading")
    return raw, after


def _metadata(path: Path, raw: bytes, info: os.stat_result) -> dict[str, Any]:
    return {
        "path": str(path.absolute()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "mtime_ns": info.st_mtime_ns,
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": f"{stat.S_IMODE(info.st_mode):04o}",
        "nlink": info.st_nlink,
    }


def _bearer_value(raw_env: bytes) -> str:
    try:
        lines = raw_env.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ClientConfigSealError("legacy bearer source is invalid") from exc
    values: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() != LEGACY_BEARER_KEY:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values.append(value)
    if len(values) != 1 or not values[0] or any(
        character.isspace() for character in values[0]
    ):
        raise ClientConfigSealError("legacy bearer source is invalid")
    return values[0]


def _inspect_client_config(
    *,
    claude_config: Path,
    codex_config: Path,
    legacy_env: Path,
) -> tuple[dict[str, Any], str]:
    claude_raw, claude_info = _read_stable_regular(claude_config)
    codex_raw, codex_info = _read_stable_regular(codex_config)
    legacy_raw, legacy_info = _read_stable_regular(legacy_env)
    try:
        claude = json.loads(claude_raw)
        claude_entry = claude["mcpServers"][CLAUDE_CLIENT_KEY]
        authorization = claude_entry["headers"]["Authorization"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ClientConfigSealError(
            "Claude MCP client configuration is invalid"
        ) from exc
    if (
        not isinstance(claude_entry, dict)
        or claude_entry.get("type") != "http"
        or claude_entry.get("url") != EXPECTED_ENDPOINT
        or not isinstance(authorization, str)
        or not authorization.startswith("Bearer ")
        or not authorization[7:]
        or any(character.isspace() for character in authorization[7:])
    ):
        raise ClientConfigSealError("Claude MCP client configuration is invalid")
    try:
        codex = tomllib.loads(codex_raw.decode("utf-8"))
        codex_entry = codex["mcp_servers"][CODEX_CLIENT_KEY]
    except (KeyError, TypeError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ClientConfigSealError(
            "Codex MCP client configuration is invalid"
        ) from exc
    if (
        not isinstance(codex_entry, dict)
        or codex_entry.get("url") != EXPECTED_ENDPOINT
        or codex_entry.get("bearer_token_env_var") != CODEX_BEARER_ENV
    ):
        raise ClientConfigSealError("Codex MCP client configuration is invalid")
    bearer = authorization[7:]
    if _bearer_value(legacy_raw) != bearer:
        raise ClientConfigSealError("configured bearer sources do not match")
    bearer_raw = bearer.encode("utf-8")
    state = {
        "kind": SEAL_KIND,
        "version": SEAL_VERSION,
        "endpoint": EXPECTED_ENDPOINT,
        "clients": {
            "claude_key": CLAUDE_CLIENT_KEY,
            "codex_key": CODEX_CLIENT_KEY,
            "codex_bearer_env_var": CODEX_BEARER_ENV,
        },
        "files": {
            "claude": _metadata(claude_config, claude_raw, claude_info),
            "codex": _metadata(codex_config, codex_raw, codex_info),
            "legacy_env": _metadata(legacy_env, legacy_raw, legacy_info),
        },
        "bearer": {
            "scheme": "Bearer",
            "value_bytes": len(bearer_raw),
            "sha256": hashlib.sha256(bearer_raw).hexdigest(),
            "sources_match": True,
        },
    }
    return state, authorization


def capture_client_config_state(
    *,
    claude_config: Path,
    codex_config: Path,
    legacy_env: Path,
) -> dict[str, Any]:
    """Return the secret-free state that must remain byte-for-byte stable."""

    state, _authorization = _inspect_client_config(
        claude_config=claude_config,
        codex_config=codex_config,
        legacy_env=legacy_env,
    )
    return state


def verified_authorization_header(
    expected: dict[str, Any],
    *,
    claude_config: Path,
    codex_config: Path,
    legacy_env: Path,
) -> str:
    """Return the header only after every sealed byte and parsed selector matches."""

    current, authorization = _inspect_client_config(
        claude_config=claude_config,
        codex_config=codex_config,
        legacy_env=legacy_env,
    )
    if expected != current:
        raise ClientConfigSealError("client configuration differs from its seal")
    return authorization


def _write_once(path: Path, payload: bytes) -> None:
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        created = True
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o400)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise


def write_client_config_seal(
    *,
    seal_path: Path,
    pin_path: Path,
    claude_config: Path,
    codex_config: Path,
    legacy_env: Path,
) -> dict[str, Any]:
    """Publish a secret-free write-once seal plus an external digest pin."""

    if seal_path.parent != pin_path.parent or seal_path == pin_path:
        raise ClientConfigSealError("client configuration seal paths are invalid")
    if (
        seal_path.exists()
        or seal_path.is_symlink()
        or pin_path.exists()
        or pin_path.is_symlink()
    ):
        raise ClientConfigSealError("client configuration seal already exists")
    state = capture_client_config_state(
        claude_config=claude_config,
        codex_config=codex_config,
        legacy_env=legacy_env,
    )
    raw = (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    pin = f"{hashlib.sha256(raw).hexdigest()}  {seal_path.name}\n".encode("ascii")
    created: list[Path] = []
    try:
        _write_once(seal_path, raw)
        created.append(seal_path)
        _write_once(pin_path, pin)
        created.append(pin_path)
    except BaseException:
        for path in reversed(created):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    return state


def _read_write_once(path: Path) -> bytes:
    raw, info = _read_stable_regular(path)
    if stat.S_IMODE(info.st_mode) != 0o400 or info.st_nlink != 1:
        raise ClientConfigSealError("client configuration seal is not write-once")
    return raw


def read_pinned_client_authorization(
    *,
    seal_path: Path,
    pin_path: Path,
    claude_config: Path,
    codex_config: Path,
    legacy_env: Path,
) -> str:
    """Verify the external pin and live bytes, then return the in-memory header."""

    seal_raw = _read_write_once(seal_path)
    pin_raw = _read_write_once(pin_path)
    expected_pin = (
        f"{hashlib.sha256(seal_raw).hexdigest()}  {seal_path.name}\n".encode("ascii")
    )
    if pin_raw != expected_pin:
        raise ClientConfigSealError("client configuration seal pin is invalid")
    try:
        expected = json.loads(seal_raw)
    except json.JSONDecodeError as exc:
        raise ClientConfigSealError("client configuration seal is invalid") from exc
    if not isinstance(expected, dict):
        raise ClientConfigSealError("client configuration seal is invalid")
    return verified_authorization_header(
        expected,
        claude_config=claude_config,
        codex_config=codex_config,
        legacy_env=legacy_env,
    )
