"""Canonical, owner-bound runtime for deterministic daemons (no LLM provider).

Only the operator CLI creates an identity. Runtime calls cannot register,
recover, retire, change a binding, or read another mailbox. Credentials stay
inside the helper. Existing proxy and enrollment contracts remain authoritative.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import runpy
import secrets
import stat
import threading
import time
from urllib.parse import urlsplit
import uuid

from agentstack_mail import enrollment_cli as enroll
from agentstack_mail.enrollment import credential_fingerprint, PROTOCOL_VERSION
from .agent_mail_client import AgentMailClient, HttpJsonRpcTransport
from .mcp_server import AgentStackProxy

KIND = "orrery-daemon-binding-v1"
NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,127}")


class DaemonError(RuntimeError):
    """Only fixed reason codes cross the helper boundary."""


def private_text(path):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise DaemonError("private-file-unsafe")
        data = os.read(fd, 1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise DaemonError("private-file-too-large")
        return data.decode("utf-8")
    finally:
        os.close(fd)


def private_json(path):
    value = json.loads(private_text(path))
    if not isinstance(value, dict):
        raise DaemonError("profile-invalid")
    return value


def connection_at(path):
    path = Path(path)
    value = private_json(path)
    if value.get("kind") != "orrery-mail-connection-v1":
        raise DaemonError("connection-invalid")
    instance = value.get("expected_server_instance_id")
    if not isinstance(instance, str) or str(uuid.UUID(instance)) != instance:
        raise DaemonError("connection-not-pinned")
    url = urlsplit(value.get("mcp_url", ""))
    if (
        url.scheme != "http"
        or url.hostname not in {"127.0.0.1", "::1"}
        or url.username is not None
        or url.password is not None
        or url.fragment
    ):
        raise DaemonError("connection-not-local")
    # Current bundled Mail is loopback local-principal, with owner tokens for
    # writes. No ambient bearer, env-file substitution or raw fallback.
    if value.get("http_bearer_mode") != "disabled":
        raise DaemonError("connection-bearer-unsupported")
    for key in ("runtime_dir", "management_socket"):
        if not isinstance(value.get(key), str) or not Path(value[key]).is_absolute():
            raise DaemonError("connection-invalid")
    runtime = Path(value["runtime_dir"])
    info = runtime.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise DaemonError("runtime-directory-unsafe")
    return value


def profile_at(path):
    path = Path(path)
    value = private_json(path)
    fields = {
        "kind",
        "name",
        "agent_id",
        "project_key",
        "connection",
        "peer_name",
        "peer_id",
        "pause_path",
        "lock_path",
    }
    if set(value) != fields or value.get("kind") != KIND:
        raise DaemonError("profile-invalid")
    for key in ("name", "peer_name"):
        if not isinstance(value[key], str) or NAME.fullmatch(value[key]) is None:
            raise DaemonError("profile-invalid")
    for key in ("agent_id", "peer_id"):
        if type(value[key]) is not int or value[key] <= 0:
            raise DaemonError("profile-invalid")
    for key in ("project_key", "connection", "pause_path", "lock_path"):
        if not isinstance(value[key], str) or not Path(value[key]).is_absolute():
            raise DaemonError("profile-invalid")
    if value["name"] == value["peer_name"]:
        raise DaemonError("profile-invalid")
    return value


def canonical_wait():
    # Reuse the blessed primitive itself; no independent notification scanner.
    helper = Path(__file__).resolve().parents[4] / "bin/agentstack-await-reply"
    return runpy.run_path(str(helper))["wait_for_reply"]


class DaemonRuntime:
    def __init__(
        self, profile, connection, token, *, client=None, inspect=None, wait=None
    ):
        self.profile, self.connection = dict(profile), dict(connection)
        self._token = token
        self._closed = threading.Event()
        self._calls = threading.RLock()
        self._wait_lock = threading.Lock()
        self._wait = wait or canonical_wait()
        self._after_id = 0
        self._generation = None
        self._lock_fd = None
        self._inspect = inspect or self._management_inspect
        if client is None:
            transport = HttpJsonRpcTransport(
                connection["mcp_url"], timeout=2, allow_redirects=False
            )

            def guarded_transport(payload):
                self._active()
                return transport(payload)

            client = AgentMailClient(guarded_transport)
        self.client = client
        self.proxy = AgentStackProxy(None, None, self.client)
        self.proxy.bind_direct(
            agent_name=profile["name"],
            project_key=profile["project_key"],
            owner_token=token,
            program="orrery-daemon",
        )
        self.sid = self.proxy.bound_session_id

    @classmethod
    def open(cls, path):
        value = profile_at(path)
        connection = connection_at(value["connection"])
        parent = Path(value["lock_path"]).parent
        info = parent.stat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise DaemonError("lock-directory-unsafe")
        fd = os.open(value["lock_path"], os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise DaemonError("lock-file-unsafe")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Helper reads its own owner credential; models never receive it.
            token = private_text(
                Path(connection["runtime_dir"]) / f"agent_token_{value['name']}"
            ).strip()
            if not 20 <= len(token) <= 64:
                raise DaemonError("credential-unavailable")
            runtime = cls(value, connection, token)
            runtime._lock_fd = fd
            runtime.call("runtime_status", {})
            return runtime
        except BaseException:
            os.close(fd)
            raise

    def _active(self):
        if self._closed.is_set():
            raise DaemonError("daemon-closed")
        if Path(self.profile["pause_path"]).exists():
            raise DaemonError("daemon-paused")

    def _management_inspect(self):
        self._active()
        return enroll._request(
            Path(self.connection["management_socket"]),
            {
                "version": PROTOCOL_VERSION,
                "action": "inspect",
                "project_key": self.profile["project_key"],
                "agent_id": self.profile["agent_id"],
                "expected_name": self.profile["name"],
            },
            timeout=2,
        )

    def _verify(self):
        self._active()
        inspected = self._inspect()
        expected = {
            "ok": True,
            "server_instance_id": self.connection["expected_server_instance_id"],
            "project_key": self.profile["project_key"],
            "agent_id": self.profile["agent_id"],
            "name": self.profile["name"],
            "retired": False,
            "credential_state": "server-token",
            "credential_fingerprint": credential_fingerprint(self._token),
        }
        if not isinstance(inspected, dict) or any(
            inspected.get(k) != v for k, v in expected.items()
        ):
            raise DaemonError("daemon-binding-mismatch")
        if (
            type(inspected.get("agent_id")) is not int
            or inspected.get("retired") is not False
        ):
            raise DaemonError("daemon-binding-mismatch")
        generation = inspected.get("credential_generation")
        if type(generation) is not int or generation < 0:
            raise DaemonError("daemon-credential-mismatch")
        if self._generation is not None and self._generation != generation:
            raise DaemonError("daemon-credential-mismatch")
        self._active()
        health = self.client._call_tool_object("health_check", {})
        if (
            health.get("status") != "ok"
            or health.get("server_instance_id") != expected["server_instance_id"]
        ):
            raise DaemonError("daemon-http-instance-mismatch")
        self._active()
        own = self.proxy.whois(self.sid, name=self.profile["name"])
        if (
            type(own.get("id")) is not int
            or own.get("id") != self.profile["agent_id"]
            or own.get("name") != self.profile["name"]
            or own.get("program") != "orrery-daemon"
            or own.get("retired_at")
            or type(own.get("project_id")) is not int
            or own["project_id"] <= 0
        ):
            raise DaemonError("daemon-row-mismatch")
        self._generation = generation
        return own["project_id"]

    def call(self, name, arguments):
        try:
            return self._call(name, arguments)
        except DaemonError:
            raise
        except Exception:
            raise DaemonError("daemon-operation-failed") from None

    def _call(self, name, arguments):
        allowed = {
            "runtime_status": set(),
            "whois": {"agent_name"},
            "fetch_inbox": {"limit", "include_bodies", "since_ts"},
            "acknowledge_message": {"message_id"},
            "send_message": {"to", "subject", "body_md", "thread_id"},
        }
        if (
            name not in allowed
            or not isinstance(arguments, dict)
            or set(arguments) - allowed[name]
        ):
            raise DaemonError("daemon-call-denied")
        with self._calls:
            project_id = self._verify()
            self._active()
            if name == "runtime_status":
                return {
                    "name": self.profile["name"],
                    "project": self.profile["project_key"],
                    "agent_id": self.profile["agent_id"],
                    "instance": self.connection["expected_server_instance_id"],
                }
            if name == "whois":
                if arguments.get("agent_name") != self.profile["peer_name"]:
                    raise DaemonError("daemon-peer-denied")
                result = self.proxy.whois(self.sid, name=self.profile["peer_name"])
                if (
                    type(result.get("id")) is not int
                    or result.get("id") != self.profile["peer_id"]
                    or result.get("project_id") != project_id
                    or result.get("retired_at")
                ):
                    raise DaemonError("daemon-peer-changed")
                return result
            if name == "fetch_inbox":
                limit = arguments.get("limit", 20)
                bodies = arguments.get("include_bodies", False)
                since = arguments.get("since_ts")
                if (
                    type(limit) is not int
                    or not 1 <= limit <= 1000
                    or type(bodies) is not bool
                    or (
                        since is not None
                        and (not isinstance(since, str) or len(since) > 128)
                    )
                ):
                    raise DaemonError("daemon-arguments-invalid")
                # The model-facing proxy retains its 100-item limit. The
                # deterministic daemon uses the canonical Mail 1,000-item window.
                result = self.client.fetch_inbox(
                    project_key=self.profile["project_key"],
                    agent_name=self.profile["name"],
                    registration_token=self._token,
                    limit=limit,
                    include_bodies=bodies,
                    since_ts=since,
                )
                for item in result:
                    if (
                        type(item.get("id")) is not int
                        or item["id"] <= 0
                        or item.get("project_id") != project_id
                        or item.get("from") != self.profile["peer_name"]
                        or type(item.get("sender_id")) is not int
                        or item.get("sender_id") != self.profile["peer_id"]
                    ):
                        raise DaemonError("daemon-message-denied")
                if bodies and result:
                    self._after_id = max(self._after_id, max(x["id"] for x in result))
                return result
            if name == "acknowledge_message":
                return self.proxy.acknowledge_message(self.sid, **arguments)
            if arguments.get("to") != [self.profile["peer_name"]]:
                raise DaemonError("daemon-peer-denied")
            # Check the actual peer's row just before using its name for a write.
            self._active()
            peer = self.proxy.whois(self.sid, name=self.profile["peer_name"])
            if (
                type(peer.get("id")) is not int
                or peer.get("id") != self.profile["peer_id"]
                or peer.get("project_id") != project_id
                or peer.get("retired_at")
            ):
                raise DaemonError("daemon-peer-changed")
            self._active()
            return self.proxy.send_message(self.sid, **arguments)

    def wait_for_notification(self, timeout):
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not 0 < timeout <= 60
        ):
            raise DaemonError("daemon-timeout-invalid")
        if not self._wait_lock.acquire(blocking=False):
            raise DaemonError("daemon-wait-already-active")
        try:
            self._active()
            code, message = self._wait(
                lambda: self.call(
                    "fetch_inbox", {"limit": 1000, "include_bodies": False}
                ),
                timeout=timeout,
                after_id=self._after_id,
                cancelled=self._closed,
            )
            if code not in {0, 124, 125}:
                raise DaemonError("daemon-wait-failed")
            return message is not None
        except DaemonError:
            if self._closed.is_set():
                return False
            raise
        finally:
            self._wait_lock.release()

    def close(self):
        deadline = time.monotonic() + 4.5
        self._closed.set()
        # Idle wait is cancelled immediately; network is bounded to 2 seconds.
        if not self._calls.acquire(timeout=4.5):
            raise DaemonError("daemon-close-timeout")
        self._calls.release()
        if not self._wait_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise DaemonError("daemon-close-timeout")
        try:
            if not self._calls.acquire(timeout=max(0, deadline - time.monotonic())):
                raise DaemonError("daemon-close-timeout")
            try:
                if self._lock_fd is not None:
                    os.close(self._lock_fd)
                    self._lock_fd = None
            finally:
                self._calls.release()
        finally:
            self._wait_lock.release()


def create_identity(connection_path, *, project_key, name, journal_path, client=None):
    """Explicit local operator command only. Never automatically retries a write.

    A fsynced journal precedes the sole register attempt. Unknown outcomes retain
    its credential for operator recovery and refuse another creation attempt.
    """
    if not NAME.fullmatch(name) or not Path(project_key).is_absolute():
        raise DaemonError("creation-arguments-invalid")
    connection = connection_at(connection_path)
    client = client or AgentMailClient(
        HttpJsonRpcTransport(connection["mcp_url"], timeout=2, allow_redirects=False)
    )
    health = client._call_tool_object("health_check", {})
    if (
        health.get("server_instance_id") != connection["expected_server_instance_id"]
        or health.get("status") != "ok"
    ):
        raise DaemonError("daemon-http-instance-mismatch")
    journal_path = Path(journal_path)
    if not journal_path.is_absolute():
        raise DaemonError("creation-journal-invalid")
    info = journal_path.parent.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise DaemonError("creation-journal-invalid")
    if journal_path.exists() or journal_path.is_symlink():
        raise DaemonError("creation-already-attempted")
    active = Path(connection["runtime_dir"]) / f"agent_token_{name}"
    if active.exists() or active.is_symlink():
        raise DaemonError("creation-local-identity-exists")
    token = secrets.token_urlsafe(32)
    journal = {
        "kind": "orrery-daemon-create-v1",
        "name": name,
        "project_key": project_key,
        "server_instance_id": connection["expected_server_instance_id"],
        "registration_token": token,
        "outcome": "unknown",
    }
    enroll._write_exclusive(journal_path, (json.dumps(journal) + "\n").encode())
    result = client._call_tool_object(
        "register_agent",
        {
            "project_key": project_key,
            "name": name,
            "program": "orrery-daemon",
            "model": "deterministic",
            "task_description": "Dedicated deterministic Mail daemon",
            "registration_token": token,
        },
    )
    if (
        result.get("name") != name
        or type(result.get("id")) is not int
        or result["id"] <= 0
        or result.get("registration_token", token) != token
    ):
        raise DaemonError("creation-response-mismatch")
    # Do not replace an existing identity's local owner credential.
    enroll._write_exclusive(active, (token + "\n").encode())
    receipt = {
        "kind": "orrery-daemon-created-v1",
        "name": name,
        "agent_id": result["id"],
        "project_key": project_key,
        "server_instance_id": connection["expected_server_instance_id"],
    }
    enroll._atomic_bytes_write(
        journal_path,
        (
            json.dumps({**journal, "outcome": "accepted", "receipt": receipt}) + "\n"
        ).encode(),
    )
    return receipt


def finalize_creation(connection_path, *, journal_path, agent_id, client=None):
    """Operator-only recovery of the same creation journal, never register again."""
    if type(agent_id) is not int or agent_id <= 0:
        raise DaemonError("creation-arguments-invalid")
    connection = connection_at(connection_path)
    journal = private_json(journal_path)
    if (
        journal.get("kind") != "orrery-daemon-create-v1"
        or journal.get("outcome") not in {"unknown", "accepted"}
        or journal.get("server_instance_id")
        != connection["expected_server_instance_id"]
        or not isinstance(journal.get("name"), str)
        or NAME.fullmatch(journal["name"]) is None
        or not isinstance(journal.get("registration_token"), str)
    ):
        raise DaemonError("creation-journal-invalid")
    inspected = enroll._request(
        Path(connection["management_socket"]),
        {
            "version": PROTOCOL_VERSION,
            "action": "inspect",
            "agent_id": agent_id,
            "project_key": journal["project_key"],
            "expected_name": journal["name"],
        },
        timeout=2,
    )
    expected = {
        "ok": True,
        "server_instance_id": journal["server_instance_id"],
        "agent_id": agent_id,
        "name": journal["name"],
        "project_key": journal["project_key"],
        "retired": False,
        "credential_state": "server-token",
        "credential_fingerprint": credential_fingerprint(journal["registration_token"]),
    }
    if not isinstance(inspected, dict) or any(
        inspected.get(k) != v for k, v in expected.items()
    ):
        raise DaemonError("creation-row-mismatch")
    if (
        type(inspected.get("agent_id")) is not int
        or inspected.get("retired") is not False
    ):
        raise DaemonError("creation-row-mismatch")
    client = client or AgentMailClient(
        HttpJsonRpcTransport(connection["mcp_url"], timeout=2, allow_redirects=False)
    )
    health = client._call_tool_object("health_check", {})
    own = client.whois(
        project_key=journal["project_key"],
        agent_name=journal["name"],
        registration_token=journal["registration_token"],
    )
    if (
        health.get("status") != "ok"
        or health.get("server_instance_id") != journal["server_instance_id"]
        or type(own.get("id")) is not int
        or own.get("id") != agent_id
        or own.get("program") != "orrery-daemon"
        or own.get("retired_at")
    ):
        raise DaemonError("creation-row-mismatch")
    active = Path(connection["runtime_dir"]) / f"agent_token_{journal['name']}"
    if active.exists() or active.is_symlink():
        if private_text(active).strip() != journal["registration_token"]:
            raise DaemonError("creation-local-identity-conflict")
    else:
        enroll._write_exclusive(active, (journal["registration_token"] + "\n").encode())
    receipt = {
        "kind": "orrery-daemon-created-v1",
        "name": journal["name"],
        "agent_id": agent_id,
        "project_key": journal["project_key"],
        "server_instance_id": journal["server_instance_id"],
    }
    enroll._atomic_bytes_write(
        Path(journal_path),
        (
            json.dumps({**journal, "outcome": "accepted", "receipt": receipt}) + "\n"
        ).encode(),
    )
    return receipt
