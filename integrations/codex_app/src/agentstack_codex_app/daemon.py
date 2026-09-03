"""Codex App Bridge daemon for P1 identity and runtime telemetry.

The socket handler validates and queues events before replying, keeping the
synchronous Codex hook independent of agent-mail latency. A worker owns binding
registration, credential persistence, retry spooling, and atomic snapshots.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import secrets
import socket
import stat
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .agent_mail_client import AgentMailClient, AgentMailError, HttpJsonRpcTransport
from .delivery import DeliveryManager
from .hook_entry import (
    MAX_EVENT_BYTES,
    runtime_dir_from_env,
    session_has_codex_desktop_transcript,
    validate_event,
)
from .identity_store import (
    IdentityStore,
    IdentityStoreError,
    build_binding,
    external_id_for,
    utc_now,
)
from .snapshot import SnapshotStore, runtime_record
from .wake import (
    ExecResumeAdapter,
    WakeCoordinator,
    WakePolicy,
    validate_codex_binary,
)


RETRY_SCHEMA_VERSION = 1
RETRY_FIELDS = frozenset(
    {
        "retry_schema_version",
        "event",
        "attempt_count",
        "first_failed_at",
        "next_attempt_at",
    }
)
PROCESS_OK = "ok"
PROCESS_INELIGIBLE = "ineligible"
PROCESS_TRANSIENT_FAILURE = "transient_failure"
PROCESS_PERMANENT_FAILURE = "permanent_failure"


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
    registration_retry_max_attempts: int = 12
    registration_retry_max_age_seconds: float = 3600.0
    registration_retry_max_backoff_seconds: float = 300.0
    codex_sessions_root: Path = Path("~/.codex/sessions").expanduser()
    enforce_surface_eligibility: bool = True
    signals_dir: Path | None = None
    project_slug: str | None = None
    delivery_db_path: Path | None = None
    cold_wake_enabled: bool = True
    wake_poll_seconds: float = 0.25
    wake_coalesce_seconds: float = 2.0
    wake_lease_seconds: float = 900.0
    wake_base_backoff_seconds: float = 2.0
    wake_max_backoff_seconds: float = 300.0
    wake_max_attempts: int = 5
    wake_limit_per_hour: int = 12
    wake_timeout_seconds: float = 900.0
    codex_binary: str = "codex"
    plugin_id: str = "agentstack-codex-app@agentstack-local"
    skip_git_repo_check: bool = False
    stale_after_seconds: float = 3600.0

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
        retry_max_attempts = _env_int(
            env,
            "AGENTSTACK_CODEX_APP_RETRY_MAX_ATTEMPTS",
            12,
            minimum=1,
            maximum=100,
        )
        retry_max_age_seconds = _env_float(
            env,
            "AGENTSTACK_CODEX_APP_RETRY_MAX_AGE_SECONDS",
            3600,
            minimum=60,
            maximum=604800,
        )
        retry_max_backoff_seconds = _env_float(
            env,
            "AGENTSTACK_CODEX_APP_RETRY_MAX_BACKOFF_SECONDS",
            300,
            minimum=1,
            maximum=3600,
        )
        if retry_max_backoff_seconds < retry_seconds:
            raise ValueError(
                "AGENTSTACK_CODEX_APP_RETRY_MAX_BACKOFF_SECONDS must not be "
                "less than AGENTSTACK_CODEX_APP_RETRY_SECONDS"
            )
        codex_home = Path(
            (env.get("CODEX_HOME") or "~/.codex").strip()
        ).expanduser()
        cold_wake_enabled = _env_bool(
            env.get("AGENTSTACK_CODEX_APP_COLD_WAKE", "1"),
            "AGENTSTACK_CODEX_APP_COLD_WAKE",
        )
        wake_poll_seconds = _env_float(
            env,
            "AGENTSTACK_CODEX_APP_WAKE_POLL_SECONDS",
            0.25,
            minimum=0.05,
            maximum=5,
        )
        wake_coalesce_seconds = _env_float(
            env,
            "AGENTSTACK_CODEX_APP_WAKE_COALESCE_SECONDS",
            2,
            minimum=0,
            maximum=10,
        )
        wake_timeout_seconds = _env_float(
            env,
            "AGENTSTACK_CODEX_APP_WAKE_TIMEOUT_SECONDS",
            900,
            minimum=30,
            maximum=7200,
        )
        wake_lease_seconds = _env_float(
            env,
            "AGENTSTACK_CODEX_APP_WAKE_LEASE_SECONDS",
            900,
            minimum=30,
            maximum=7200,
        )
        wake_base_backoff_seconds = _env_float(
            env,
            "AGENTSTACK_CODEX_APP_WAKE_BASE_BACKOFF_SECONDS",
            2,
            minimum=0.1,
            maximum=300,
        )
        wake_max_backoff_seconds = _env_float(
            env,
            "AGENTSTACK_CODEX_APP_WAKE_MAX_BACKOFF_SECONDS",
            300,
            minimum=1,
            maximum=3600,
        )
        if wake_max_backoff_seconds < wake_base_backoff_seconds:
            raise ValueError(
                "AGENTSTACK_CODEX_APP_WAKE_MAX_BACKOFF_SECONDS must not be "
                "less than the base backoff"
            )
        wake_limit_per_hour = _env_int(
            env,
            "AGENTSTACK_CODEX_APP_WAKE_LIMIT_PER_HOUR",
            12,
            minimum=1,
            maximum=120,
        )
        wake_max_attempts = _env_int(
            env,
            "AGENTSTACK_CODEX_APP_WAKE_MAX_ATTEMPTS",
            5,
            minimum=1,
            maximum=20,
        )
        delivery_value = (
            env.get("AGENTSTACK_CODEX_APP_DELIVERY_DB") or ""
        ).strip()
        codex_binary = validate_codex_binary(
            env.get("AGENTSTACK_CODEX_BINARY") or "codex",
            setting_name="AGENTSTACK_CODEX_BINARY",
        )
        skip_git_repo_check = _env_bool(
            env.get("AGENTSTACK_CODEX_APP_SKIP_GIT_CHECK", "0"),
            "AGENTSTACK_CODEX_APP_SKIP_GIT_CHECK",
        )
        plugin_id = (
            env.get("AGENTSTACK_CODEX_APP_PLUGIN_ID")
            or "agentstack-codex-app@agentstack-local"
        ).strip()
        stale_after_seconds = _env_float(
            env,
            "AGENTSTACK_CODEX_APP_STALE_AFTER_SECONDS",
            3600,
            minimum=300,
            maximum=604800,
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
            registration_retry_max_attempts=retry_max_attempts,
            registration_retry_max_age_seconds=retry_max_age_seconds,
            registration_retry_max_backoff_seconds=retry_max_backoff_seconds,
            codex_sessions_root=codex_home / "sessions",
            signals_dir=(
                Path(signals_value).expanduser()
                if signals_value
                else Path(mail_home or "~/.agentstack/mail").expanduser() / "signals"
            ),
            project_slug=project_slug,
            delivery_db_path=(
                Path(delivery_value).expanduser()
                if delivery_value
                else runtime_dir / "delivery.sqlite3"
            ),
            cold_wake_enabled=cold_wake_enabled,
            wake_poll_seconds=wake_poll_seconds,
            wake_coalesce_seconds=wake_coalesce_seconds,
            wake_lease_seconds=wake_lease_seconds,
            wake_base_backoff_seconds=wake_base_backoff_seconds,
            wake_max_backoff_seconds=wake_max_backoff_seconds,
            wake_max_attempts=wake_max_attempts,
            wake_limit_per_hour=wake_limit_per_hour,
            wake_timeout_seconds=wake_timeout_seconds,
            codex_binary=codex_binary,
            plugin_id=plugin_id,
            skip_git_repo_check=skip_git_repo_check,
            stale_after_seconds=stale_after_seconds,
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
        wake_coordinator: WakeCoordinator | None = None,
    ) -> None:
        self.config = config
        self.agent_mail = agent_mail
        self.identities = identity_store or IdentityStore(config.runtime_dir / "identity")
        self.snapshots = snapshot_store or SnapshotStore(config.snapshot_path)
        self.wake_coordinator = wake_coordinator
        if (
            self.wake_coordinator is None
            and config.cold_wake_enabled
            and config.signals_dir is not None
        ):
            delivery_path = (
                config.delivery_db_path
                if config.delivery_db_path is not None
                else config.runtime_dir / "delivery.sqlite3"
            )
            self.wake_coordinator = WakeCoordinator(
                DeliveryManager(delivery_path),
                self.identities,
                self.snapshots,
                ExecResumeAdapter(
                    codex_binary=config.codex_binary,
                    plugin_id=config.plugin_id,
                    skip_git_repo_check=config.skip_git_repo_check,
                ),
                signals_dir=config.signals_dir,
                project_slug=lambda project_key: (
                    config.project_slug or _project_slug(project_key)
                ),
                policy=WakePolicy(
                    coalesce_seconds=config.wake_coalesce_seconds,
                    lease_seconds=config.wake_lease_seconds,
                    base_backoff_seconds=config.wake_base_backoff_seconds,
                    max_backoff_seconds=config.wake_max_backoff_seconds,
                    max_attempts=config.wake_max_attempts,
                    wakes_per_hour=config.wake_limit_per_hour,
                    process_timeout_seconds=config.wake_timeout_seconds,
                ),
            )
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
        outcome = self._process_normalized_event(normalized)
        if outcome == PROCESS_TRANSIENT_FAILURE and enqueue_on_failure:
            self._append_retry(normalized, attempt_count=1)
        return external_id

    def _process_normalized_event(self, normalized: Mapping[str, Any]) -> str:
        """Process one validated event and classify its retry disposition."""

        if not self._event_is_eligible(normalized):
            _diagnostic(
                "event_dropped",
                reason="ineligible_surface",
                session_id=normalized["session_id"],
            )
            return PROCESS_INELIGIBLE
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
                agent_name=_provisional_agent_name(external_id),
                project_key=self.config.project_key,
            )
            binding = self.identities.save(binding)
            self.identities.store_owner_token(external_id, owner_token)
        else:
            binding = self.identities.touch(external_id)
            owner_token = self.identities.load_owner_token(external_id)
            if owner_token is None:
                self._update_snapshot(binding, normalized, state="degraded")
                return PROCESS_PERMANENT_FAILURE

        if should_register:
            try:
                registration = self.agent_mail.register_agent(
                    **self._registration_arguments(
                        binding,
                        model=normalized.get("model") or "unknown",
                        owner_token=owner_token,
                    )
                )
                binding = self._adopt_registered_name(
                    binding,
                    registration.agent_name,
                )
            except AgentMailError:
                self._update_snapshot(binding, normalized, state="degraded")
                return PROCESS_TRANSIENT_FAILURE

        self._update_snapshot(binding, normalized, state=_state_for_event(normalized))
        return PROCESS_OK

    def _event_is_eligible(self, event: Mapping[str, Any]) -> bool:
        if not self.config.enforce_surface_eligibility:
            return True
        return session_has_codex_desktop_transcript(
            event["session_id"],
            sessions_root=self.config.codex_sessions_root,
        )

    def reconcile_bindings(self) -> int:
        """Refresh persisted identities from authoritative register responses."""

        reconciled = 0
        for binding in self.identities.list_bindings():
            if self.config.enforce_surface_eligibility and not (
                session_has_codex_desktop_transcript(
                    binding["session_id"],
                    sessions_root=self.config.codex_sessions_root,
                )
            ):
                continue
            try:
                owner_token = self.identities.load_owner_token(
                    binding["external_id"]
                )
                if owner_token is None:
                    continue
                previous = self.snapshots.get(binding["external_id"])
                registration = self.agent_mail.register_agent(
                    **self._registration_arguments(
                        binding,
                        model=(
                            str(previous.get("model") or "unknown")
                            if previous is not None
                            else "unknown"
                        ),
                        owner_token=owner_token,
                    )
                )
                updated = self._adopt_registered_name(
                    binding,
                    registration.agent_name,
                )
            except (AgentMailError, IdentityStoreError, OSError, ValueError):
                continue
            if previous is not None and updated["agent_name"] != binding["agent_name"]:
                self.snapshots.upsert(
                    runtime_record(
                        updated,
                        previous,
                        state=previous["state"],
                        last_seen_at=previous["last_seen_at"],
                        delivery=previous["delivery"],
                    )
                )
            reconciled += 1
        return reconciled

    @staticmethod
    def _registration_arguments(
        binding: Mapping[str, Any],
        *,
        model: str,
        owner_token: str,
    ) -> dict[str, Any]:
        """Build registration arguments without exporting provisional names."""

        arguments: dict[str, Any] = {
            "project_key": binding["project_key"],
            "model": model,
            "registration_token": owner_token,
            "task_description": (
                "Codex App subagent"
                if binding["agent_id"] is not None
                else "Codex App root task"
            ),
        }
        if not _is_provisional_agent_name(binding["agent_name"]):
            arguments["agent_name"] = binding["agent_name"]
        return arguments

    def _adopt_registered_name(
        self,
        binding: Mapping[str, Any],
        registered_name: str,
    ) -> dict[str, Any]:
        try:
            return self.identities.reconcile_agent_name(
                binding["external_id"],
                registered_name,
            )
        except IdentityStoreError as exc:
            raise AgentMailError(
                "register_agent returned an invalid or conflicting agent name"
            ) from exc

    def replay_spool(
        self,
        path: Path,
        *,
        enqueue_on_failure: bool | None = None,
    ) -> int:
        """Replay bounded retry records, accepting legacy raw event rows."""

        if not path.exists():
            return 0
        drain = path.with_name(
            f".{path.name}.drain-{os.getpid()}-{time.time_ns()}"
        )
        try:
            os.replace(path, drain)
        except FileNotFoundError:
            return 0
        return self._replay_retry_file(drain)

    def _replay_retry_file(self, path: Path) -> int:
        processed = 0
        dropped = 0
        retained = 0
        completed = False
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = self._normalize_retry_record(json.loads(line))
                    except (ValueError, json.JSONDecodeError):
                        dropped += 1
                        continue
                    event = record["event"]
                    if not self._event_is_eligible(event):
                        dropped += 1
                        continue
                    if self._retry_expired(record):
                        dropped += 1
                        continue
                    if _parse_timestamp(record["next_attempt_at"]) > _utc_datetime():
                        _append_jsonl(record, self.config.retry_path)
                        retained += 1
                        continue
                    if self._retry_binding_is_retired(event):
                        dropped += 1
                        continue
                    outcome = self._process_normalized_event(event)
                    processed += 1
                    if outcome == PROCESS_TRANSIENT_FAILURE:
                        next_attempt = record["attempt_count"] + 1
                        if next_attempt >= self.config.registration_retry_max_attempts:
                            dropped += 1
                            continue
                        self._append_retry(
                            event,
                            attempt_count=next_attempt,
                            first_failed_at=record["first_failed_at"],
                        )
                        retained += 1
                    elif outcome != PROCESS_OK:
                        dropped += 1
            completed = True
        finally:
            if completed:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        _diagnostic(
            "retry_replay",
            processed=processed,
            retained=retained,
            dropped=dropped,
        )
        return processed

    def _normalize_retry_record(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("retry row must be an object")
        if set(payload) == RETRY_FIELDS:
            if payload.get("retry_schema_version") != RETRY_SCHEMA_VERSION:
                raise ValueError("unsupported retry schema")
            event = validate_event(payload.get("event"))
            attempt_count = payload.get("attempt_count")
            if (
                not isinstance(attempt_count, int)
                or isinstance(attempt_count, bool)
                or attempt_count < 1
            ):
                raise ValueError("invalid retry attempt count")
            first_failed_at = _parse_timestamp(payload.get("first_failed_at"))
            next_attempt_at = _parse_timestamp(payload.get("next_attempt_at"))
            return {
                "retry_schema_version": RETRY_SCHEMA_VERSION,
                "event": event,
                "attempt_count": attempt_count,
                "first_failed_at": _format_timestamp(first_failed_at),
                "next_attempt_at": _format_timestamp(next_attempt_at),
            }
        event = validate_event(payload)
        now = _utc_datetime()
        return {
            "retry_schema_version": RETRY_SCHEMA_VERSION,
            "event": event,
            "attempt_count": 1,
            "first_failed_at": _format_timestamp(now),
            "next_attempt_at": _format_timestamp(now),
        }

    def _retry_expired(self, record: Mapping[str, Any]) -> bool:
        if record["attempt_count"] >= self.config.registration_retry_max_attempts:
            return True
        age = _utc_datetime() - _parse_timestamp(record["first_failed_at"])
        return age.total_seconds() >= self.config.registration_retry_max_age_seconds

    def _append_retry(
        self,
        event: Mapping[str, Any],
        *,
        attempt_count: int,
        first_failed_at: str | None = None,
    ) -> None:
        now = _utc_datetime()
        first = (
            _parse_timestamp(first_failed_at)
            if first_failed_at is not None
            else now
        )
        exponent = min(20, max(0, attempt_count - 1))
        delay = min(
            self.config.registration_retry_seconds * (2**exponent),
            self.config.registration_retry_max_backoff_seconds,
        )
        record = {
            "retry_schema_version": RETRY_SCHEMA_VERSION,
            "event": validate_event(event),
            "attempt_count": attempt_count,
            "first_failed_at": _format_timestamp(first),
            "next_attempt_at": _format_timestamp(now + timedelta(seconds=delay)),
        }
        if self._retry_expired(record):
            _diagnostic(
                "retry_dropped",
                reason="limit_reached",
                session_id=record["event"]["session_id"],
            )
            return
        _append_jsonl(record, self.config.retry_path)

    def _retry_binding_is_retired(self, event: Mapping[str, Any]) -> bool:
        external_id = external_id_for(event["session_id"], event["agent_id"])
        binding = self.identities.resolve(external_id)
        if binding is None:
            return False
        try:
            profile = self.agent_mail.whois(
                project_key=binding["project_key"],
                agent_name=binding["agent_name"],
                registration_token=self.identities.load_owner_token(
                    binding["external_id"]
                ),
            )
        except (AgentMailError, OSError, ValueError):
            return False
        retired_at = profile.get("retired_at")
        return isinstance(retired_at, str) and bool(retired_at.strip())

    def queue_spool(self, path: Path) -> int:
        """Move a spool aside and queue valid events without blocking the socket."""

        if not path.exists():
            return 0
        drain = path.with_name(
            f".{path.name}.drain-{os.getpid()}-{time.time_ns()}"
        )
        try:
            os.replace(path, drain)
        except FileNotFoundError:
            return 0
        return self._queue_event_file(drain)

    def _queue_event_file(self, path: Path) -> int:
        queued = 0
        completed = False
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            continue
                        self._events.put(validate_event(event))
                        queued += 1
                    except (ValueError, json.JSONDecodeError):
                        continue
            completed = True
        finally:
            if completed:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        return queued

    def recover_stale_drains(self) -> tuple[int, int]:
        """Recover files left when a previous daemon stopped during replay."""

        retry_rows = 0
        hook_rows = 0
        retry_pattern = f".{self.config.retry_path.name}.drain-*"
        hook_pattern = f".{self.config.spool_path.name}.drain-*"
        for path in sorted(self.config.retry_path.parent.glob(retry_pattern)):
            try:
                retry_rows += self._replay_retry_file(path)
            except OSError:
                _diagnostic("drain_file_failed", kind="retry")
        for path in sorted(self.config.spool_path.parent.glob(hook_pattern)):
            try:
                hook_rows += self._queue_event_file(path)
            except OSError:
                _diagnostic("drain_file_failed", kind="hook")
        _diagnostic(
            "drain_recovery",
            retry_rows=retry_rows,
            hook_rows=hook_rows,
        )
        return retry_rows, hook_rows

    def serve_forever(self) -> None:
        """Run the private Unix socket until :meth:`stop` is called."""

        self._prepare_runtime()
        _diagnostic("bridge_start", pid=os.getpid())
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
                _diagnostic("bridge_stop", pid=os.getpid())

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
        try:
            self.recover_stale_drains()
        except (OSError, ValueError):
            _diagnostic("drain_recovery_failed", reason="local_io")
        try:
            self.reconcile_bindings()
        except (OSError, ValueError):
            # Startup reconciliation repairs old names but must not make the
            # lifecycle socket unavailable when local state is corrupt.
            pass
        try:
            self.snapshots.mark_waiting_dormant_older_than(
                self.config.stale_after_seconds
            )
        except (OSError, ValueError):
            pass
        next_retry = time.monotonic()
        next_wake = time.monotonic()
        while True:
            now = time.monotonic()
            deadlines = [next_retry]
            if self.wake_coordinator is not None:
                deadlines.append(next_wake)
            timeout = max(0.0, min(deadlines) - now)
            try:
                event = self._events.get(timeout=timeout)
            except queue.Empty:
                now = time.monotonic()
                if now >= next_retry:
                    self.replay_spool(self.config.retry_path)
                    try:
                        self.snapshots.mark_waiting_dormant_older_than(
                            self.config.stale_after_seconds
                        )
                    except (OSError, ValueError):
                        pass
                    next_retry = now + self.config.registration_retry_seconds
                if self.wake_coordinator is not None and now >= next_wake:
                    try:
                        self.wake_coordinator.tick(self.identities.list_bindings())
                    except Exception:
                        # Cold wake is an optional delivery boundary. A broken
                        # adapter or state DB must not stop lifecycle telemetry.
                        pass
                    next_wake = now + self.config.wake_poll_seconds
                continue
            if event is None:
                break
            try:
                self.process_event(event)
            except (ValueError, OSError):
                _diagnostic(
                    "event_dropped",
                    reason="local_processing_error",
                    session_id=str(event.get("session_id") or "unknown"),
                )
            now = time.monotonic()
            if self.wake_coordinator is not None and now >= next_wake:
                try:
                    self.wake_coordinator.tick(self.identities.list_bindings())
                except Exception:
                    # Keep the Bridge alive even when cold wake is degraded.
                    pass
                next_wake = now + self.config.wake_poll_seconds

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
                delivery=(
                    previous.get("delivery")
                    if previous is not None
                    else None
                ),
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


@dataclass(frozen=True, slots=True)
class CleanupFailure:
    external_id: str
    agent_name: str
    error_code: str


@dataclass(frozen=True, slots=True)
class CleanupReport:
    cleaned: tuple[dict[str, Any], ...]
    failures: tuple[CleanupFailure, ...]


def cleanup_orphan_bindings(
    identities: IdentityStore,
    snapshots: SnapshotStore,
    agent_mail: AgentMailClient,
    *,
    sessions_root: str | os.PathLike[str],
) -> CleanupReport:
    """Retire/purge orphan bindings while isolating per-binding failures."""

    cleaned: list[dict[str, Any]] = []
    failures: list[CleanupFailure] = []
    for binding in identities.list_bindings():
        if session_has_codex_desktop_transcript(
            binding["session_id"],
            sessions_root=sessions_root,
        ):
            continue
        # Load the owner token before the first whois: a token-strict server
        # refuses an unauthenticated read of another agent's profile.
        try:
            owner_token = identities.load_owner_token(binding["external_id"])
        except (IdentityStoreError, OSError):
            failures.append(_cleanup_failure(binding, "owner_token_unreadable"))
            continue
        retired = _remote_agent_is_retired(agent_mail, binding, owner_token)
        if not retired:
            if owner_token is None:
                failures.append(_cleanup_failure(binding, "owner_token_missing"))
                continue
            try:
                agent_mail.retire_agent(
                    project_key=binding["project_key"],
                    agent_name=binding["agent_name"],
                    registration_token=owner_token,
                )
                retired = True
            except (AgentMailError, OSError, ValueError):
                # Re-read after failure to handle an administrative/racing retire.
                retired = _remote_agent_is_retired(agent_mail, binding, owner_token)
                if not retired:
                    failures.append(_cleanup_failure(binding, "retire_failed"))
                    continue
        try:
            snapshots.remove(binding["external_id"])
            identities.delete(binding["external_id"])
        except (IdentityStoreError, OSError, ValueError):
            failures.append(_cleanup_failure(binding, "local_purge_failed"))
            continue
        cleaned.append(binding)
    return CleanupReport(tuple(cleaned), tuple(failures))


def _remote_agent_is_retired(
    agent_mail: AgentMailClient,
    binding: Mapping[str, Any],
    owner_token: str | None = None,
) -> bool:
    try:
        profile = agent_mail.whois(
            project_key=binding["project_key"],
            agent_name=binding["agent_name"],
            registration_token=owner_token,
        )
    except (AgentMailError, OSError, ValueError):
        return False
    retired_at = profile.get("retired_at")
    return isinstance(retired_at, str) and bool(retired_at.strip())


def _cleanup_failure(
    binding: Mapping[str, Any],
    error_code: str,
) -> CleanupFailure:
    return CleanupFailure(
        external_id=binding["external_id"],
        agent_name=binding["agent_name"],
        error_code=error_code,
    )


def _provisional_agent_name(external_id: str) -> str:
    """Create a local-only display name until agent-mail assigns the identity."""

    digest = hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:12]
    return f"Pending-{digest}"


def _is_provisional_agent_name(agent_name: str) -> bool:
    return re.fullmatch(r"Pending-[0-9a-f]{12}", agent_name) is not None


def _project_slug(project_key: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", project_key.strip().lower()).strip("-")
    return slug or "project"


def _env_bool(value: str, name: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _env_float(
    env: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = (env.get(name) or str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _env_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = (env.get(name) or str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _utc_datetime() -> datetime:
    return datetime.now(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _append_jsonl(payload: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        encoded = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        os.write(descriptor, encoded)
    finally:
        os.close(descriptor)


def _diagnostic(event: str, **fields: Any) -> None:
    payload = {
        "schema_version": 1,
        "event": event,
        "timestamp": _format_timestamp(_utc_datetime()),
        **fields,
    }
    try:
        print(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            file=sys.stderr,
            flush=True,
        )
    except OSError:
        pass


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
