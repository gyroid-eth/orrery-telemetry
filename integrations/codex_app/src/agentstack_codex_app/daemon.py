"""Codex App Bridge daemon for P1 identity and runtime telemetry.

The socket handler validates and queues events before replying, keeping the
synchronous Codex hook independent of agent-mail latency. A worker owns binding
registration, credential persistence, retry spooling, and atomic snapshots.
"""

from __future__ import annotations

import json
import os
import queue
import re
import secrets
import socket
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .agent_mail_client import AgentMailClient, AgentMailError, HttpJsonRpcTransport
from .hook_entry import (
    MAX_EVENT_BYTES,
    append_spool,
    runtime_dir_from_env,
    validate_event,
)
from .identity_store import IdentityStore, build_binding, external_id_for, utc_now
from .snapshot import SnapshotStore, runtime_record


_ADJECTIVES = (
    "Black",
    "Blue",
    "Bold",
    "Brave",
    "Bright",
    "Brown",
    "Calm",
    "Cloudy",
    "Curious",
    "Dark",
    "Foggy",
    "Frosty",
    "Gold",
    "Gray",
    "Green",
    "Navy",
    "Noble",
    "Orange",
    "Pink",
    "Purple",
    "Quiet",
    "Rainy",
    "Red",
    "Sharp",
    "Silver",
    "Stormy",
    "Sunny",
    "Swift",
    "Wild",
    "Windy",
    "White",
)
_SCIENTISTS = (
    "Bohr",
    "Boltzmann",
    "Carson",
    "Chandrasekhar",
    "Curie",
    "Darwin",
    "Einstein",
    "Euler",
    "Faraday",
    "Fermi",
    "Franklin",
    "Galilei",
    "Gauss",
    "Goodall",
    "Hopper",
    "Hubble",
    "Jemison",
    "Kepler",
    "Lavoisier",
    "Lovelace",
    "Maxwell",
    "McClintock",
    "Meitner",
    "Mendel",
    "Newton",
    "Noether",
    "Pasteur",
    "Raman",
    "Sagan",
    "Somerville",
    "Turing",
    "Yukawa",
)


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    runtime_dir: Path
    socket_path: Path
    spool_path: Path
    retry_path: Path
    snapshot_path: Path
    project_key: str
    agent_mail_endpoint: str
    agent_mail_bearer: str | None = None
    registration_retry_seconds: float = 5.0
    signals_dir: Path | None = None
    project_slug: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "BridgeConfig":
        env = os.environ if environ is None else environ
        runtime_dir = runtime_dir_from_env(env)
        project_key = (env.get("AGENTSTACK_PROJECT_KEY") or "").strip()
        endpoint = (env.get("AGENTSTACK_MCP_URL") or "").strip()
        if not project_key or not Path(project_key).is_absolute():
            raise ValueError("AGENTSTACK_PROJECT_KEY must be an absolute path")
        if not endpoint:
            raise ValueError("AGENTSTACK_MCP_URL must be configured")
        socket_value = (env.get("AGENTSTACK_CODEX_APP_SOCKET") or "").strip()
        spool_value = (env.get("AGENTSTACK_CODEX_APP_SPOOL") or "").strip()
        signals_value = (env.get("AGENTSTACK_SIGNALS_DIR") or "").strip()
        mail_home = (env.get("AGENTSTACK_MAIL_HOME") or "").strip()
        project_slug = (env.get("AGENTSTACK_PROJECT_SLUG") or "").strip() or None
        if project_slug is not None and re.fullmatch(
            r"[a-z0-9][a-z0-9-]*", project_slug
        ) is None:
            raise ValueError("AGENTSTACK_PROJECT_SLUG has an invalid format")
        retry_value = (env.get("AGENTSTACK_CODEX_APP_RETRY_SECONDS") or "5").strip()
        try:
            retry_seconds = float(retry_value)
        except ValueError as exc:
            raise ValueError(
                "AGENTSTACK_CODEX_APP_RETRY_SECONDS must be numeric"
            ) from exc
        if not 1 <= retry_seconds <= 300:
            raise ValueError(
                "AGENTSTACK_CODEX_APP_RETRY_SECONDS must be between 1 and 300"
            )
        return cls(
            runtime_dir=runtime_dir,
            socket_path=Path(socket_value).expanduser() if socket_value else runtime_dir / "bridge.sock",
            spool_path=Path(spool_value).expanduser() if spool_value else runtime_dir / "hook-events.jsonl",
            retry_path=runtime_dir / "registration-retry.jsonl",
            snapshot_path=runtime_dir / "snapshot.json",
            project_key=project_key,
            agent_mail_endpoint=endpoint,
            agent_mail_bearer=(env.get("MCP_AGENT_MAIL_TOKEN") or None),
            registration_retry_seconds=retry_seconds,
            signals_dir=(
                Path(signals_value).expanduser()
                if signals_value
                else Path(mail_home or "~/.mcp_agent_mail").expanduser() / "signals"
            ),
            project_slug=project_slug,
        )


class BridgeDaemon:
    """Receive hook events and maintain durable Codex App runtime identity."""

    def __init__(
        self,
        config: BridgeConfig,
        agent_mail: AgentMailClient,
        *,
        identity_store: IdentityStore | None = None,
        snapshot_store: SnapshotStore | None = None,
        name_factory: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self.agent_mail = agent_mail
        self.identities = identity_store or IdentityStore(config.runtime_dir / "identity")
        self.snapshots = snapshot_store or SnapshotStore(config.snapshot_path)
        self.name_factory = name_factory or _fresh_agent_name
        self._events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._pending_fingerprints: dict[str, tuple[tuple[str, int, int], ...]] = {}

    def process_event(
        self, event: Mapping[str, Any], *, enqueue_on_failure: bool = True
    ) -> str:
        """Synchronously register/update one identity and its snapshot."""

        normalized = validate_event(event)
        external_id = external_id_for(
            normalized["session_id"], normalized["agent_id"]
        )
        binding = self.identities.resolve(external_id)
        previous = self.snapshots.get(external_id)
        should_register = binding is None or normalized["hook_event_name"] in {
            "SessionStart",
            "SubagentStart",
        } or (previous is not None and previous["state"] == "degraded")
        if binding is None:
            owner_token = secrets.token_urlsafe(32)
            binding = build_binding(
                session_id=normalized["session_id"],
                agent_id=normalized["agent_id"],
                agent_name=self.name_factory(),
                project_key=self.config.project_key,
            )
            binding = self.identities.save(binding)
            self.identities.store_owner_token(external_id, owner_token)
        else:
            binding = self.identities.touch(external_id)
            owner_token = self.identities.load_owner_token(external_id)
            if owner_token is None:
                self._update_snapshot(binding, normalized, state="degraded")
                return external_id

        if should_register:
            try:
                registration = self.agent_mail.register_agent(
                    project_key=binding["project_key"],
                    model=normalized.get("model") or "unknown",
                    registration_token=owner_token,
                    agent_name=binding["agent_name"],
                    task_description=(
                        "Codex App subagent"
                        if binding["agent_id"] is not None
                        else "Codex App root task"
                    ),
                )
                if registration.agent_name != binding["agent_name"]:
                    raise AgentMailError("registered identity name changed")
            except AgentMailError:
                self._update_snapshot(binding, normalized, state="degraded")
                if enqueue_on_failure:
                    append_spool(normalized, self.config.retry_path)
                return external_id

        self._update_snapshot(binding, normalized, state=_state_for_event(normalized))
        return external_id

    def replay_spool(self, path: Path, *, enqueue_on_failure: bool) -> int:
        """Move a JSONL spool aside, replay valid entries, then remove it."""

        if not path.exists():
            return 0
        drain = path.with_name(f".{path.name}.drain-{os.getpid()}")
        try:
            os.replace(path, drain)
        except FileNotFoundError:
            return 0
        processed = 0
        try:
            with drain.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            continue
                        self.process_event(
                            event, enqueue_on_failure=enqueue_on_failure
                        )
                        processed += 1
                    except (ValueError, OSError, json.JSONDecodeError):
                        continue
        finally:
            try:
                drain.unlink()
            except FileNotFoundError:
                pass
        return processed

    def queue_spool(self, path: Path) -> int:
        """Move a spool aside and queue valid events without blocking the socket."""

        if not path.exists():
            return 0
        drain = path.with_name(f".{path.name}.drain-{os.getpid()}")
        try:
            os.replace(path, drain)
        except FileNotFoundError:
            return 0
        queued = 0
        try:
            with drain.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            continue
                        self._events.put(validate_event(event))
                        queued += 1
                    except (ValueError, json.JSONDecodeError):
                        continue
        finally:
            try:
                drain.unlink()
            except FileNotFoundError:
                pass
        return queued

    def serve_forever(self) -> None:
        """Run the private Unix socket until :meth:`stop` is called."""

        self._prepare_runtime()
        self._start_worker()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(os.fspath(self.config.socket_path))
            os.chmod(self.config.socket_path, 0o600)
            listener.listen(32)
            listener.settimeout(0.25)
            self.queue_spool(self.config.spool_path)
            try:
                while not self._stop.is_set():
                    try:
                        connection, _ = listener.accept()
                    except socket.timeout:
                        continue
                    with connection:
                        self._handle_connection(connection)
            finally:
                self.stop()
                self._remove_socket()

    def stop(self) -> None:
        self._stop.set()
        if self._worker is not None and self._worker.is_alive():
            self._events.put(None)
            self._worker.join(timeout=2)

    def _handle_connection(self, connection: socket.socket) -> None:
        try:
            payload = _recv_payload(connection)
            event = validate_event(payload)
            self._events.put(event)
            response = {
                "ok": True,
                "external_id": external_id_for(event["session_id"], event["agent_id"]),
            }
            pending = self._pending_notice(event)
            if pending is not None:
                response["pending"] = pending
        except (ValueError, OSError, json.JSONDecodeError):
            response = {"ok": False, "error": "invalid runtime event"}
        try:
            connection.sendall(
                json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"
            )
        except OSError:
            pass

    def _pending_notice(
        self, event: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Return one coalesced PostToolUse notice for new signal files."""

        if event["hook_event_name"] != "PostToolUse":
            return None
        external_id = external_id_for(event["session_id"], event["agent_id"])
        binding = self.identities.resolve(external_id)
        if binding is None:
            return None
        fingerprint = self._pending_signal_fingerprint(binding)
        if not fingerprint:
            self._pending_fingerprints.pop(external_id, None)
            return None
        if self._pending_fingerprints.get(external_id) == fingerprint:
            return None
        self._pending_fingerprints[external_id] = fingerprint
        return {
            "count": len(fingerprint),
            "agent_name": binding["agent_name"],
            "project_key": binding["project_key"],
        }

    def _pending_signal_fingerprint(
        self, binding: Mapping[str, Any]
    ) -> tuple[tuple[str, int, int], ...]:
        signals_dir = self.config.signals_dir
        if signals_dir is None:
            return ()
        project_slug = self.config.project_slug or _project_slug(
            binding["project_key"]
        )
        agents_dir = signals_dir / "projects" / project_slug / "agents"
        legacy = agents_dir / f"{binding['agent_name']}.signal"
        per_message = agents_dir / binding["agent_name"]
        candidates = [legacy]
        if per_message.is_dir():
            candidates.extend(sorted(per_message.glob("*.signal")))
        observed: list[tuple[str, int, int]] = []
        for path in candidates:
            try:
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    continue
                if metadata.st_size > 16 * 1024:
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    continue
                if payload.get("agent") != binding["agent_name"]:
                    continue
                if payload.get("project") != project_slug:
                    continue
                observed.append((path.name, metadata.st_mtime_ns, metadata.st_size))
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                continue
        return tuple(observed)

    def _start_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="agentstack-codex-app-bridge",
            daemon=True,
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        next_retry = time.monotonic()
        while True:
            timeout = max(0.0, next_retry - time.monotonic())
            try:
                event = self._events.get(timeout=timeout)
            except queue.Empty:
                self.replay_spool(
                    self.config.retry_path,
                    enqueue_on_failure=True,
                )
                next_retry = time.monotonic() + self.config.registration_retry_seconds
                continue
            if event is None:
                break
            try:
                self.process_event(event)
            except (ValueError, OSError):
                try:
                    append_spool(event, self.config.retry_path)
                except OSError:
                    pass

    def _update_snapshot(
        self,
        binding: Mapping[str, Any],
        event: Mapping[str, Any],
        *,
        state: str,
    ) -> None:
        previous = self.snapshots.get(binding["external_id"])
        snapshot_event = dict(event)
        if previous is not None:
            snapshot_event["model"] = snapshot_event.get("model") or previous.get("model")
            snapshot_event["cwd"] = snapshot_event.get("cwd") or previous.get("cwd")
        self.snapshots.upsert(
            runtime_record(
                binding,
                snapshot_event,
                state=state,
                last_seen_at=utc_now(),
            )
        )

    def _prepare_runtime(self) -> None:
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config.runtime_dir, 0o700)
        self.config.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config.socket_path.parent, 0o700)
        if len(os.fsencode(self.config.socket_path)) >= 100:
            raise OSError("Bridge socket path is too long for portable AF_UNIX use")
        if self.config.socket_path.exists():
            mode = self.config.socket_path.lstat().st_mode
            if not stat.S_ISSOCK(mode):
                raise OSError("refusing to replace non-socket bridge path")
            self.config.socket_path.unlink()

    def _remove_socket(self) -> None:
        try:
            if stat.S_ISSOCK(self.config.socket_path.lstat().st_mode):
                self.config.socket_path.unlink()
        except FileNotFoundError:
            pass


def _state_for_event(event: Mapping[str, Any]) -> str:
    if event["hook_event_name"] in {"UserPromptSubmit", "PostToolUse"}:
        return "working"
    return "waiting"


def _fresh_agent_name() -> str:
    """Create one non-descriptive adjective+scientist identity candidate."""

    return f"{secrets.choice(_ADJECTIVES)}-{secrets.choice(_SCIENTISTS)}"


def _project_slug(project_key: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", project_key.strip().lower()).strip("-")
    return slug or "project"


def _recv_payload(connection: socket.socket) -> dict[str, Any]:
    chunks = bytearray()
    while len(chunks) <= MAX_EVENT_BYTES:
        block = connection.recv(min(4096, MAX_EVENT_BYTES + 1 - len(chunks)))
        if not block:
            break
        chunks.extend(block)
        if b"\n" in block:
            break
    if len(chunks) > MAX_EVENT_BYTES:
        raise OSError("runtime event is too large")
    payload = json.loads(bytes(chunks).split(b"\n", 1)[0])
    if not isinstance(payload, dict):
        raise ValueError("runtime event must be an object")
    return payload


def serve() -> None:
    """Build the configured Bridge and serve until interrupted."""

    config = BridgeConfig.from_env()
    transport = HttpJsonRpcTransport(
        config.agent_mail_endpoint,
        bearer_token=config.agent_mail_bearer,
    )
    BridgeDaemon(config, AgentMailClient(transport)).serve_forever()


if __name__ == "__main__":
    serve()
