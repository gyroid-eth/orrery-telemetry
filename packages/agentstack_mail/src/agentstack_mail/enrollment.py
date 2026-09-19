"""Local operator control plane for persistent-agent credential enrollment.

This protocol is deliberately absent from the MCP registry.  It is served on
an owner-only Unix socket by the Mail process and returns receipts, never
credential material.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import stat
import struct
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from .db import get_engine, get_session
from .models import (
    Agent,
    EnrollmentAudit,
    EnrollmentRequest,
    MailInstance,
    Project,
)

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 16 * 1024
_OPERATIONS = frozenset({"claim", "recover"})


def credential_fingerprint(value: str | None) -> str | None:
    """Return a non-secret, stable diagnostic fingerprint."""

    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _receipt(record: EnrollmentRequest) -> dict[str, Any]:
    return {
        "kind": "orrery-enrollment-receipt-v1",
        "request_id": record.request_id,
        "server_instance_id": record.server_instance_id,
        "project_key": record.project_key,
        "agent_id": record.agent_id,
        "operation": record.operation,
        "result": record.result,
        "reason": record.reason,
        "old_generation": record.old_generation,
        "new_generation": record.new_generation,
        "old_fingerprint": record.old_fingerprint,
        "new_fingerprint": record.new_fingerprint,
    }


def _response(*, ok: bool, reason: str, **values: Any) -> dict[str, Any]:
    return {"version": PROTOCOL_VERSION, "ok": ok, "reason": reason, **values}


def _valid_request_id(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError):
        return False


def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
    peer = writer.get_extra_info("socket")
    if peer is None:
        return None
    getpeereid = getattr(peer, "getpeereid", None)
    if callable(getpeereid):
        try:
            uid, _gid = getpeereid()
            return int(uid)
        except OSError:
            return None
    if hasattr(socket, "SO_PEERCRED"):
        try:
            raw = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            return int(uid)
        except OSError:
            return None
    if hasattr(socket, "LOCAL_PEERCRED"):
        try:
            # Darwin's xucred begins with cr_version (u_int32), then cr_uid.
            raw = peer.getsockopt(0, socket.LOCAL_PEERCRED, 128)
            return int(struct.unpack_from("=I", raw, 4)[0])
        except (OSError, struct.error):
            return None
    return None


async def ensure_mail_instance() -> MailInstance:
    """Return the stable identity row, creating it once for a fresh database."""

    async with get_session() as session:
        async with session.begin():
            row = await session.get(MailInstance, 1)
            if row is None:
                row = MailInstance(id=1, instance_id=str(uuid.uuid4()))
                session.add(row)
        return row


async def inspect_target(
    *, instance_id: str, project_key: str, agent_id: int, expected_name: str | None
) -> dict[str, Any]:
    async with get_session() as session:
        project = (
            await session.execute(select(Project).where(Project.human_key == project_key))
        ).scalar_one_or_none()
        if project is None:
            return _response(ok=False, reason="project-not-found", server_instance_id=instance_id)
        agent = await session.get(Agent, agent_id)
        if agent is None or agent.project_id != project.id:
            return _response(ok=False, reason="agent-not-found", server_instance_id=instance_id)
        if expected_name is not None and agent.name != expected_name:
            return _response(
                ok=False,
                reason="name-mismatch",
                server_instance_id=instance_id,
                agent_id=agent_id,
                actual_name=agent.name,
            )
        token = agent.registration_token
        return _response(
            ok=True,
            reason="inspected",
            server_instance_id=instance_id,
            project_key=project.human_key,
            agent_id=agent_id,
            name=agent.name,
            credential_state="server-token" if token else "server-null",
            credential_generation=int(agent.credential_generation or 0),
            credential_fingerprint=credential_fingerprint(token),
            retired=agent.retired_at is not None,
        )


def _same_request(record: EnrollmentRequest, request: dict[str, Any], fingerprint: str) -> bool:
    return (
        record.expected_server_instance_id == request.get("expected_server_instance_id")
        and record.project_key == request.get("project_key")
        and record.agent_id == request.get("agent_id")
        and record.expected_name == request.get("expected_name")
        and record.operation == request.get("action")
        and record.expected_generation == request.get("expected_generation")
        and record.new_fingerprint == fingerprint
    )


async def _record_result(
    *,
    session: Any,
    request: dict[str, Any],
    peer_uid: int,
    instance_id: str,
    fingerprint: str,
    result: str,
    reason: str,
    old_generation: int,
    new_generation: int,
    old_fingerprint: str | None,
) -> EnrollmentRequest:
    record = EnrollmentRequest(
        request_id=request["request_id"],
        server_instance_id=instance_id,
        expected_server_instance_id=request["expected_server_instance_id"],
        project_key=request["project_key"],
        agent_id=request["agent_id"],
        expected_name=request.get("expected_name"),
        operation=request["action"],
        expected_generation=request["expected_generation"],
        new_fingerprint=fingerprint,
        result=result,
        reason=reason,
        old_generation=old_generation,
        new_generation=new_generation,
        old_fingerprint=old_fingerprint,
    )
    session.add(record)
    session.add(
        EnrollmentAudit(
            request_id=record.request_id,
            peer_uid=peer_uid,
            server_instance_id=instance_id,
            project_key=record.project_key,
            agent_id=record.agent_id,
            operation=record.operation,
            old_generation=old_generation,
            new_generation=new_generation,
            old_fingerprint=old_fingerprint,
            new_fingerprint=fingerprint,
            reason=reason,
            result=result,
        )
    )
    return record


async def apply_enrollment(
    *, instance_id: str, request: dict[str, Any], peer_uid: int
) -> dict[str, Any]:
    """Apply one idempotent claim/recover request and return only a receipt."""

    operation = request["action"]
    fingerprint = credential_fingerprint(request["new_credential"])
    assert fingerprint is not None

    async with get_session() as session:
        async with session.begin():
            existing = await session.get(EnrollmentRequest, request["request_id"])
            if existing is not None:
                if _same_request(existing, request, fingerprint):
                    return _response(ok=existing.result == "accepted", reason="replayed", receipt=_receipt(existing))
                session.add(
                    EnrollmentAudit(
                        request_id=request["request_id"],
                        peer_uid=peer_uid,
                        server_instance_id=instance_id,
                        project_key=request["project_key"],
                        agent_id=request["agent_id"],
                        operation=operation,
                        old_generation=0,
                        new_generation=0,
                        new_fingerprint=fingerprint,
                        reason="request-conflict",
                        result="rejected",
                    )
                )
                return _response(ok=False, reason="request-conflict")

            project = (
                await session.execute(
                    select(Project).where(Project.human_key == request["project_key"])
                )
            ).scalar_one_or_none()
            agent = await session.get(Agent, request["agent_id"])
            old_generation = int(agent.credential_generation or 0) if agent is not None else 0
            old_fingerprint = credential_fingerprint(agent.registration_token) if agent is not None else None

            reason: str | None = None
            if request["expected_server_instance_id"] != instance_id:
                reason = "server-identity-mismatch"
            elif project is None:
                reason = "project-not-found"
            elif agent is None or agent.project_id != project.id:
                reason = "agent-not-found"
            elif request.get("expected_name") is not None and agent.name != request["expected_name"]:
                reason = "name-mismatch"
            elif agent.retired_at is not None:
                reason = "agent-retired"
            elif old_generation != request["expected_generation"]:
                reason = "generation-conflict"
            elif operation == "claim" and agent.registration_token is not None:
                reason = "credential-state-conflict"
            elif operation == "recover" and agent.registration_token is None:
                reason = "credential-state-conflict"

            if reason is not None:
                record = await _record_result(
                    session=session,
                    request=request,
                    peer_uid=peer_uid,
                    instance_id=instance_id,
                    fingerprint=fingerprint,
                    result="rejected",
                    reason=reason,
                    old_generation=old_generation,
                    new_generation=old_generation,
                    old_fingerprint=old_fingerprint,
                )
                return _response(ok=False, reason=reason, receipt=_receipt(record))

            assert project is not None and agent is not None
            new_generation = old_generation + 1
            conditions = [
                Agent.id == agent.id,
                Agent.project_id == project.id,
                Agent.credential_generation == old_generation,
            ]
            if operation == "claim":
                conditions.append(Agent.registration_token.is_(None))
            else:
                conditions.append(Agent.registration_token.is_not(None))
            changed = await session.execute(
                update(Agent)
                .where(*conditions)
                .values(
                    registration_token=request["new_credential"],
                    credential_generation=new_generation,
                )
            )
            if changed.rowcount != 1:
                # This is the CAS losing branch.  No credential changed in this
                # transaction; only the fixed rejection audit is committed.
                current = await session.get(Agent, agent.id, populate_existing=True)
                observed_generation = int(current.credential_generation or 0) if current else old_generation
                observed_fingerprint = credential_fingerprint(current.registration_token) if current else old_fingerprint
                record = await _record_result(
                    session=session,
                    request=request,
                    peer_uid=peer_uid,
                    instance_id=instance_id,
                    fingerprint=fingerprint,
                    result="rejected",
                    reason="generation-conflict",
                    old_generation=observed_generation,
                    new_generation=observed_generation,
                    old_fingerprint=observed_fingerprint,
                )
                return _response(ok=False, reason="generation-conflict", receipt=_receipt(record))

            record = await _record_result(
                session=session,
                request=request,
                peer_uid=peer_uid,
                instance_id=instance_id,
                fingerprint=fingerprint,
                result="accepted",
                reason=f"operator-{operation}",
                old_generation=old_generation,
                new_generation=new_generation,
                old_fingerprint=old_fingerprint,
            )
            return _response(ok=True, reason="accepted", receipt=_receipt(record))


class EnrollmentControlServer:
    """Owner-only, one-request-per-connection Unix socket server."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path.expanduser()
        self.instance_id = ""
        self._server: asyncio.AbstractServer | None = None
        self._mutation_lock = asyncio.Lock()

    async def start(self) -> None:
        if not self.socket_path.is_absolute():
            raise RuntimeError("management socket path must be absolute")
        if len(os.fsencode(self.socket_path)) > 100:
            raise RuntimeError("management socket path exceeds the portable Unix limit")
        if get_engine().echo:
            raise RuntimeError("management socket requires database echo logging to be disabled")
        parent = self.socket_path.parent
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if parent.stat().st_uid != os.getuid():
            raise RuntimeError("management socket directory has a different owner")
        parent.chmod(0o700)
        if self.socket_path.exists() or self.socket_path.is_symlink():
            existing = self.socket_path.lstat()
            if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid():
                raise RuntimeError("management socket path is not an owned socket")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.2)
                probe.connect(str(self.socket_path))
            except (ConnectionRefusedError, FileNotFoundError):
                pass
            else:
                raise RuntimeError("management socket is already active")
            finally:
                probe.close()
            self.socket_path.unlink()
        self.instance_id = (await ensure_mail_instance()).instance_id
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))
        self.socket_path.chmod(0o600)

    async def close(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        with suppress(FileNotFoundError):
            current = self.socket_path.lstat()
            if stat.S_ISSOCK(current.st_mode) and current.st_uid == os.getuid():
                self.socket_path.unlink()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            peer_uid = _peer_uid(writer)
            if peer_uid is None:
                response = _response(ok=False, reason="peer-credential-unavailable")
            elif peer_uid != os.getuid():
                response = _response(ok=False, reason="peer-uid-mismatch")
            else:
                raw = await reader.readline()
                if not raw or len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                    response = _response(ok=False, reason="invalid-request")
                else:
                    try:
                        request = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        response = _response(ok=False, reason="invalid-request")
                    else:
                        try:
                            response = await self._dispatch(request, peer_uid)
                        except Exception:
                            # SQLAlchemy exceptions can include bound parameter
                            # values.  Never allow them to reach asyncio's
                            # exception handler, logs, or the wire.
                            response = _response(ok=False, reason="management-operation-failed")
            writer.write(json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
            with suppress(ConnectionError):
                await writer.drain()
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def _dispatch(self, request: object, peer_uid: int) -> dict[str, Any]:
        if not isinstance(request, dict) or request.get("version") != PROTOCOL_VERSION:
            return _response(ok=False, reason="invalid-request")
        action = request.get("action")
        if action == "request_status":
            request_id = request.get("request_id")
            if not _valid_request_id(request_id):
                return _response(ok=False, reason="invalid-request")
            async with get_session() as session:
                record = await session.get(EnrollmentRequest, request_id)
                if record is None:
                    return _response(ok=False, reason="request-not-found")
                return _response(ok=record.result == "accepted", reason="status", receipt=_receipt(record))
        if action == "inspect":
            if not isinstance(request.get("project_key"), str) or not request["project_key"]:
                return _response(ok=False, reason="invalid-request")
            if not isinstance(request.get("agent_id"), int) or request["agent_id"] < 1:
                return _response(ok=False, reason="invalid-request")
            expected_name = request.get("expected_name")
            if expected_name is not None and (not isinstance(expected_name, str) or not expected_name):
                return _response(ok=False, reason="invalid-request")
            return await inspect_target(
                instance_id=self.instance_id,
                project_key=request["project_key"],
                agent_id=request["agent_id"],
                expected_name=expected_name,
            )
        if action not in _OPERATIONS:
            return _response(ok=False, reason="invalid-request")
        required = {
            "request_id": str,
            "expected_server_instance_id": str,
            "project_key": str,
            "agent_id": int,
            "expected_generation": int,
            "new_credential": str,
        }
        if any(not isinstance(request.get(key), kind) for key, kind in required.items()):
            return _response(ok=False, reason="invalid-request")
        if (
            not _valid_request_id(request["request_id"])
            or not request["expected_server_instance_id"]
            or not request["project_key"]
            or request["agent_id"] < 1
            or request["expected_generation"] < 0
            or not 20 <= len(request["new_credential"]) <= 64
        ):
            return _response(ok=False, reason="invalid-request")
        expected_name = request.get("expected_name")
        if expected_name is not None and (not isinstance(expected_name, str) or not expected_name):
            return _response(ok=False, reason="invalid-request")
        async with self._mutation_lock:
            return await apply_enrollment(instance_id=self.instance_id, request=request, peer_uid=peer_uid)
