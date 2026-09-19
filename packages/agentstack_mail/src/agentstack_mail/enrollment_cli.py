"""Operator CLI for the local persistent-agent enrollment control plane."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import socket
import stat
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .enrollment import MAX_REQUEST_BYTES, PROTOCOL_VERSION, credential_fingerprint


class EnrollmentCliError(RuntimeError):
    """Fixed-code CLI failure that never carries credential material."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentstack-enroll",
        description=(
            "Inspect a persistent identity, explicitly claim/recover it, verify the "
            "receipt, then start its agentstack-persistent profile. This local operator "
            "command is never exposed as an MCP/proxy tool."
        ),
        epilog=(
            "Workflow: inspect -> claim (server-null) or recover (server-token/local-missing) "
            "-> verify the receipt -> start the persistent profile. See docs/persistent-agents.md."
        ),
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for operation in ("inspect", "claim", "recover"):
        command = subparsers.add_parser(
            operation,
            help=(
                "show the numeric target and credential state without secrets"
                if operation == "inspect"
                else (
                    "claim an existing server-null row with a new local credential"
                    if operation == "claim"
                    else "replace a server credential only when its local credential is lost"
                )
            ),
        )
        command.add_argument("--agent-id", type=int, required=True, metavar="N", help="existing numeric agent row ID")
        command.add_argument("--project-key", required=True, metavar="PATH", help="exact project human_key")
        command.add_argument(
            "--connection",
            required=True,
            metavar="PROFILE",
            help="connection profile JSON path, or a name under ~/.agentstack/connections",
        )
        command.add_argument(
            "--name",
            dest="expected_name",
            metavar="NAME",
            help="optional expected canonical name; mismatch is fail-closed and never creates an alias",
        )
        if operation == "inspect":
            command.add_argument(
                "--pin-server",
                metavar="INSTANCE_ID",
                help=(
                    "after comparing this explicit instance ID with the live inspect result, "
                    "pin it into the connection profile; claim/recover require this pin"
                ),
            )
    return parser


def _profile_path(value: str) -> Path:
    supplied = Path(value).expanduser()
    if supplied.is_absolute() or "/" in value or value.endswith(".json"):
        return supplied.resolve(strict=False)
    home = Path(os.environ.get("AGENTSTACK_HOME", "~/.agentstack")).expanduser()
    return (home / "connections" / f"{value}.json").resolve(strict=False)


def _load_profile(value: str) -> tuple[Path, dict[str, Any]]:
    path = _profile_path(value)
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise EnrollmentCliError("connection-profile-missing") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise EnrollmentCliError("connection-profile-unsafe")
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnrollmentCliError("connection-profile-invalid") from exc
    if not isinstance(profile, dict) or profile.get("kind") != "orrery-mail-connection-v1":
        raise EnrollmentCliError("connection-profile-invalid")
    socket_value = profile.get("management_socket")
    if not isinstance(socket_value, str) or not Path(socket_value).expanduser().is_absolute():
        raise EnrollmentCliError("connection-profile-invalid")
    expected = profile.get("expected_server_instance_id")
    if expected is not None and (not isinstance(expected, str) or not expected):
        raise EnrollmentCliError("connection-profile-invalid")
    return path, profile


def _request(socket_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) > MAX_REQUEST_BYTES:
        raise EnrollmentCliError("request-too-large")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5.0)
            client.connect(str(socket_path))
            client.sendall(encoded)
            chunks = bytearray()
            while len(chunks) <= MAX_REQUEST_BYTES:
                part = client.recv(4096)
                if not part:
                    break
                chunks.extend(part)
                if chunks.endswith(b"\n"):
                    break
    except (OSError, TimeoutError) as exc:
        raise EnrollmentCliError("mail-control-unavailable") from exc
    if not chunks.endswith(b"\n") or len(chunks) > MAX_REQUEST_BYTES:
        raise EnrollmentCliError("mail-control-invalid-response")
    try:
        response = json.loads(chunks)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EnrollmentCliError("mail-control-invalid-response") from exc
    if not isinstance(response, dict) or response.get("version") != PROTOCOL_VERSION:
        raise EnrollmentCliError("mail-control-invalid-response")
    return response


def _runtime_dir(profile: dict[str, Any]) -> Path:
    configured = profile.get("runtime_dir")
    if configured is None:
        configured = os.environ.get("AGENTSTACK_RUNTIME_DIR", "~/.agentstack/runtime")
    if not isinstance(configured, str) or not configured:
        raise EnrollmentCliError("connection-profile-invalid")
    path = Path(configured).expanduser().resolve(strict=False)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise EnrollmentCliError("runtime-dir-unsafe")
    path.chmod(0o700)
    return path


def _safe_agent_name(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise EnrollmentCliError("server-name-invalid")
    if value in {".", ".."} or "/" in value or "\x00" in value:
        raise EnrollmentCliError("server-name-invalid")
    return value


def _pending_path(
    runtime_dir: Path,
    *,
    connection_path: Path,
    server_instance_id: str,
    project_key: str,
    agent_id: int,
    operation: str,
) -> Path:
    identity = "\0".join(
        (str(connection_path), server_instance_id, project_key, str(agent_id), operation)
    )
    name = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    parent = runtime_dir / "enrollment" / "pending"
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    parent.chmod(0o700)
    return parent / f"{name}.json"


def _write_exclusive(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _atomic_bytes_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    _write_exclusive(temporary, payload)
    try:
        os.replace(temporary, path)
        path.chmod(0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _pin_connection_profile(
    path: Path, profile: dict[str, Any], instance_id: str
) -> dict[str, Any]:
    try:
        normalized = str(uuid.UUID(instance_id))
    except (ValueError, AttributeError) as exc:
        raise EnrollmentCliError("server-instance-id-invalid") from exc
    if normalized != instance_id.lower():
        raise EnrollmentCliError("server-instance-id-invalid")
    existing = profile.get("expected_server_instance_id")
    if existing is not None and existing != instance_id:
        raise EnrollmentCliError("connection-already-pinned")
    updated = {**profile, "expected_server_instance_id": instance_id}
    _atomic_bytes_write(
        path,
        (json.dumps(updated, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
    )
    return updated


def _load_pending(path: Path) -> dict[str, Any]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise EnrollmentCliError("pending-credential-unsafe")
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnrollmentCliError("pending-credential-invalid") from exc
    if not isinstance(pending, dict):
        raise EnrollmentCliError("pending-credential-invalid")
    token = pending.get("new_credential")
    request_id = pending.get("request_id")
    if not isinstance(token, str) or not 20 <= len(token) <= 64 or not isinstance(request_id, str):
        raise EnrollmentCliError("pending-credential-invalid")
    return pending


def _load_or_create_pending(path: Path, expected: dict[str, Any]) -> dict[str, Any]:
    if path.exists():
        pending = _load_pending(path)
        for key, value in expected.items():
            if pending.get(key) != value:
                raise EnrollmentCliError("pending-credential-conflict")
        return pending
    pending = {
        **expected,
        "request_id": str(uuid.uuid4()),
        "new_credential": secrets.token_urlsafe(32),
    }
    payload = (json.dumps(pending, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        _write_exclusive(path, payload)
    except FileExistsError:
        return _load_or_create_pending(path, expected)
    return pending


def _atomic_active_write(path: Path, secret: str) -> None:
    _atomic_bytes_write(path, (secret + "\n").encode("utf-8"))


def _identity_path(active_path: Path) -> Path:
    return active_path.with_name(f"{active_path.name}.identity.json")


def _read_owned_regular(path: Path, reason: str) -> str:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise EnrollmentCliError(reason) from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise EnrollmentCliError(reason)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EnrollmentCliError(reason) from exc


def _read_active(path: Path) -> str:
    value = _read_owned_regular(path, "active-credential-unsafe").strip()
    if not 20 <= len(value) <= 64:
        raise EnrollmentCliError("active-credential-invalid")
    return value


def _read_identity(path: Path) -> dict[str, Any]:
    raw = _read_owned_regular(path, "active-identity-unsafe")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EnrollmentCliError("active-identity-invalid") from exc
    if not isinstance(value, dict) or value.get("kind") != "orrery-enrollment-active-v1":
        raise EnrollmentCliError("active-identity-invalid")
    return value


def _binding_values(inspected: dict[str, Any]) -> dict[str, Any]:
    return {
        "server_instance_id": inspected["server_instance_id"],
        "project_key": inspected["project_key"],
        "agent_id": inspected["agent_id"],
        "agent_name": inspected["name"],
    }


def _same_binding(identity: dict[str, Any], inspected: dict[str, Any]) -> bool:
    return all(identity.get(key) == value for key, value in _binding_values(inspected).items())


def _local_credential_state(active_path: Path, inspected: dict[str, Any]) -> str:
    identity_path = _identity_path(active_path)
    active_exists = active_path.exists() or active_path.is_symlink()
    identity_exists = identity_path.exists() or identity_path.is_symlink()
    if not active_exists and not identity_exists:
        return "missing"
    if not active_exists or not identity_exists:
        if active_exists and not identity_exists:
            active = _read_active(active_path)
            if (
                inspected.get("credential_state") == "server-token"
                and credential_fingerprint(active) == inspected.get("credential_fingerprint")
            ):
                return "present-legacy-verified"
        if identity_exists and not active_exists:
            identity = _read_identity(identity_path)
            if _same_binding(identity, inspected):
                return "missing-token-same-identity"
        return "identity-conflict"
    active = _read_active(active_path)
    identity = _read_identity(identity_path)
    if not _same_binding(identity, inspected):
        return "identity-conflict"
    if (
        credential_fingerprint(active) != inspected.get("credential_fingerprint")
        or identity.get("credential_generation") != inspected.get("credential_generation")
        or identity.get("credential_fingerprint") != inspected.get("credential_fingerprint")
    ):
        return "identity-conflict"
    return "present"


@contextmanager
def _active_lock(runtime_dir: Path, agent_name: str) -> Iterator[None]:
    parent = runtime_dir / "enrollment" / "locks"
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    parent.chmod(0o700)
    key = hashlib.sha256(agent_name.encode("utf-8")).hexdigest()
    path = parent / f"{key}.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _validate_receipt_binding(receipt: dict[str, Any], pending: dict[str, Any]) -> None:
    expected = {
        "request_id": pending["request_id"],
        "server_instance_id": pending["expected_server_instance_id"],
        "project_key": pending["project_key"],
        "agent_id": pending["agent_id"],
        "operation": pending["operation"],
        "new_fingerprint": credential_fingerprint(pending["new_credential"]),
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise EnrollmentCliError("receipt-binding-mismatch")


def _validate_current_receipt(
    receipt: dict[str, Any], pending: dict[str, Any], inspected: dict[str, Any]
) -> None:
    _validate_receipt_binding(receipt, pending)
    if receipt.get("result") != "accepted":
        raise EnrollmentCliError("receipt-binding-mismatch")
    if (
        inspected.get("server_instance_id") != receipt.get("server_instance_id")
        or inspected.get("project_key") != receipt.get("project_key")
        or inspected.get("agent_id") != receipt.get("agent_id")
        or inspected.get("credential_state") != "server-token"
        or inspected.get("credential_generation") != receipt.get("new_generation")
        or inspected.get("credential_fingerprint") != receipt.get("new_fingerprint")
    ):
        raise EnrollmentCliError("receipt-no-longer-current")


def _activate_receipt(
    *,
    active_path: Path,
    receipt: dict[str, Any],
    pending: dict[str, Any],
    inspected: dict[str, Any],
) -> None:
    _validate_current_receipt(receipt, pending, inspected)
    identity_path = _identity_path(active_path)
    if identity_path.exists() or identity_path.is_symlink():
        identity = _read_identity(identity_path)
        if not _same_binding(identity, inspected):
            raise EnrollmentCliError("local-identity-conflict")
    if active_path.exists() or active_path.is_symlink():
        if _read_active(active_path) != pending["new_credential"]:
            raise EnrollmentCliError("local-credential-conflict")
    else:
        try:
            _atomic_active_write(active_path, pending["new_credential"])
        except OSError as exc:
            raise EnrollmentCliError("active-credential-save-failed") from exc
    identity = {
        "kind": "orrery-enrollment-active-v1",
        **_binding_values(inspected),
        "request_id": receipt["request_id"],
        "credential_generation": receipt["new_generation"],
        "credential_fingerprint": receipt["new_fingerprint"],
    }
    try:
        _atomic_bytes_write(
            identity_path,
            (json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
        )
    except OSError as exc:
        raise EnrollmentCliError("active-identity-save-failed") from exc


def _inspect(
    socket_path: Path, *, project_key: str, agent_id: int, expected_name: str | None
) -> dict[str, Any]:
    return _request(
        socket_path,
        {
            "version": PROTOCOL_VERSION,
            "action": "inspect",
            "project_key": project_key,
            "agent_id": agent_id,
            "expected_name": expected_name,
        },
    )


def _print_json(value: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")), file=stream)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.agent_id < 1:
        _parser().error("--agent-id must be a positive numeric row ID")
    try:
        connection_path, profile = _load_profile(args.connection)
        socket_path = Path(profile["management_socket"]).expanduser()
        inspected = _inspect(
            socket_path,
            project_key=args.project_key,
            agent_id=args.agent_id,
            expected_name=args.expected_name,
        )
        if not inspected.get("ok"):
            _print_json(inspected, stream=sys.stderr)
            raise SystemExit(3)
        profile_instance = profile.get("expected_server_instance_id")
        if profile_instance is not None and inspected.get("server_instance_id") != profile_instance:
            raise EnrollmentCliError("server-identity-mismatch")
        runtime_dir = _runtime_dir(profile)
        agent_name = _safe_agent_name(inspected.get("name"))
        active_path = runtime_dir / f"agent_token_{agent_name}"
        if args.operation == "inspect":
            pin_server = getattr(args, "pin_server", None)
            if pin_server is not None:
                if pin_server != inspected.get("server_instance_id"):
                    raise EnrollmentCliError("server-identity-mismatch")
                profile = _pin_connection_profile(connection_path, profile, pin_server)
                profile_instance = profile["expected_server_instance_id"]
            inspected["connection_pinned"] = profile_instance is not None
            inspected["local_credential_state"] = _local_credential_state(active_path, inspected)
            _print_json(inspected)
            return
        if profile_instance is None:
            raise EnrollmentCliError("connection-not-pinned")

        with _active_lock(runtime_dir, agent_name):
            # Re-read after acquiring the local identity lock.  Another CLI
            # may have completed a recovery while this process was waiting.
            inspected = _inspect(
                socket_path,
                project_key=args.project_key,
                agent_id=args.agent_id,
                expected_name=args.expected_name,
            )
            if not inspected.get("ok"):
                _print_json(inspected, stream=sys.stderr)
                raise SystemExit(3)
            if inspected.get("server_instance_id") != profile_instance:
                raise EnrollmentCliError("server-identity-mismatch")
            if _safe_agent_name(inspected.get("name")) != agent_name:
                raise EnrollmentCliError("name-mismatch")

            pending_path = _pending_path(
                runtime_dir,
                connection_path=connection_path,
                server_instance_id=profile_instance,
                project_key=args.project_key,
                agent_id=args.agent_id,
                operation=args.operation,
            )
            stable_expected = {
                "kind": "orrery-enrollment-pending-v1",
                "operation": args.operation,
                "connection_path": str(connection_path),
                "expected_server_instance_id": profile_instance,
                "project_key": args.project_key,
                "agent_id": args.agent_id,
                "expected_name": args.expected_name,
                "agent_name": agent_name,
            }
            if pending_path.exists():
                pending = _load_pending(pending_path)
                for key, value in stable_expected.items():
                    if pending.get(key) != value:
                        raise EnrollmentCliError("pending-credential-conflict")
                status = _request(
                    socket_path,
                    {
                        "version": PROTOCOL_VERSION,
                        "action": "request_status",
                        "request_id": pending["request_id"],
                    },
                )
                receipt = status.get("receipt")
                if isinstance(receipt, dict) and receipt.get("result") == "accepted":
                    try:
                        _activate_receipt(
                            active_path=active_path,
                            receipt=receipt,
                            pending=pending,
                            inspected=inspected,
                        )
                    except EnrollmentCliError as exc:
                        if str(exc) == "receipt-no-longer-current":
                            pending_path.unlink()
                        raise
                    pending_path.unlink()
                    _print_json(receipt)
                    return
                if isinstance(receipt, dict) and receipt.get("result") == "rejected":
                    # A bound, terminal rejection is the only status outcome
                    # that makes this pending request safe to discard.  A
                    # management/socket failure is ambiguous and must retain
                    # the exact request ID and credential for a later retry.
                    _validate_receipt_binding(receipt, pending)
                    pending_path.unlink()
                    _print_json(status, stream=sys.stderr)
                    raise SystemExit(3)
                if status.get("reason") != "request-not-found":
                    _print_json(status, stream=sys.stderr)
                    raise SystemExit(3)
            else:
                expected_state = "server-null" if args.operation == "claim" else "server-token"
                if inspected.get("credential_state") != expected_state:
                    raise EnrollmentCliError("credential-state-conflict")
                local_state = _local_credential_state(active_path, inspected)
                recoverable_token_loss = (
                    args.operation == "recover" and local_state == "missing-token-same-identity"
                )
                if local_state != "missing" and not recoverable_token_loss:
                    reason = (
                        "local-identity-conflict"
                        if local_state == "identity-conflict"
                        else "local-credential-exists"
                    )
                    raise EnrollmentCliError(reason)
                pending = _load_or_create_pending(
                    pending_path,
                    {**stable_expected, "expected_generation": inspected["credential_generation"]},
                )
            mutation = {
                "version": PROTOCOL_VERSION,
                "action": args.operation,
                "request_id": pending["request_id"],
                "expected_server_instance_id": pending["expected_server_instance_id"],
                "project_key": pending["project_key"],
                "agent_id": pending["agent_id"],
                "expected_name": pending["expected_name"],
                "expected_generation": pending["expected_generation"],
                "new_credential": pending["new_credential"],
            }
            try:
                response = _request(socket_path, mutation)
            except EnrollmentCliError:
                # The server may have committed before the connection was lost.
                response = _request(
                    socket_path,
                    {
                        "version": PROTOCOL_VERSION,
                        "action": "request_status",
                        "request_id": pending["request_id"],
                    },
                )
            receipt = response.get("receipt")
            if not response.get("ok") or not isinstance(receipt, dict) or receipt.get("result") != "accepted":
                _print_json(response, stream=sys.stderr)
                raise SystemExit(3)
            current = _inspect(
                socket_path,
                project_key=args.project_key,
                agent_id=args.agent_id,
                expected_name=args.expected_name,
            )
            if not current.get("ok") or current.get("server_instance_id") != profile_instance:
                raise EnrollmentCliError("server-identity-mismatch")
            _activate_receipt(
                active_path=active_path,
                receipt=receipt,
                pending=pending,
                inspected=current,
            )
            pending_path.unlink()
            _print_json(receipt)
    except EnrollmentCliError as exc:
        _print_json({"ok": False, "reason": str(exc)}, stream=sys.stderr)
        raise SystemExit(2) from None
    except OSError:
        _print_json({"ok": False, "reason": "local-io-failed"}, stream=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":  # pragma: no cover
    main()
