from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

from agentstack_codex_app.agent_mail_client import AgentMailError, Registration
from agentstack_codex_app.daemon import (
    BridgeConfig,
    BridgeDaemon,
    cleanup_orphan_bindings,
)
from agentstack_codex_app.delivery import DeliveryManager
from agentstack_codex_app.hook_entry import forward_event
from agentstack_codex_app.identity_store import (
    IdentityStore,
    build_binding,
    external_id_for,
)
from agentstack_codex_app.snapshot import SnapshotStore, read_snapshot, runtime_record
from agentstack_codex_app.wake import WakeCoordinator, WakePolicy


# Unix socket paths are capped near 104 bytes, so these tests need a *short*
# temp directory. macOS puts the real one at /private/tmp; Linux has no such
# path at all, and pointing at a directory that does not exist made every one
# of these fail there with FileNotFoundError. None means "use the platform
# default", which on Linux is /tmp and is already short.
SHORT_TMP_DIR = "/private/tmp" if os.path.isdir("/private/tmp") else None

class FakeAgentMail:
    def __init__(self, *, normalize_names=False, server_name="Calm-Noether"):
        self.calls = []
        self.fail = False
        self.normalize_names = normalize_names
        self.server_name = server_name
        self.retired_agents = set()
        self.retire_failures = set()

    def register_agent(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise AgentMailError("offline")
        agent_name = kwargs.get("agent_name") or self.server_name
        if self.normalize_names:
            agent_name = "".join(character for character in agent_name if character.isalnum())
        return Registration(agent_name, kwargs["registration_token"])

    def retire_agent(self, **kwargs):
        self.calls.append({"retire_agent": kwargs})
        if self.fail or kwargs["agent_name"] in self.retire_failures:
            raise AgentMailError("offline")
        self.retired_agents.add(kwargs["agent_name"])
        return {"status": "retired"}

    def whois(self, **kwargs):
        self.calls.append({"whois": kwargs})
        profile = {"name": kwargs["agent_name"], "program": "codex-app"}
        if kwargs["agent_name"] in self.retired_agents:
            profile["retired_at"] = "2026-07-16T12:00:00Z"
        return profile


class FakeWakeCoordinator:
    def __init__(self):
        self.ticks = []

    def tick(self, bindings):
        self.ticks.append(list(bindings))


class FakeResumeProcess:
    def poll(self):
        return None


class FakeResumeAdapter:
    def __init__(self):
        self.calls = []

    def start(self, session_id, messages, *, cwd=None):
        self.calls.append((session_id, tuple(messages), cwd))
        return FakeResumeProcess()


def _config(tmp_path: Path) -> BridgeConfig:
    runtime = tmp_path / "runtime"
    return BridgeConfig(
        runtime_dir=runtime,
        socket_path=runtime / "bridge.sock",
        spool_path=runtime / "hook-events.jsonl",
        retry_path=runtime / "registration-retry.jsonl",
        snapshot_path=runtime / "snapshot.json",
        project_key="/workspace/example",
        agent_mail_endpoint="http://agent-mail.invalid/api/",
        registration_retry_seconds=0,
        enforce_surface_eligibility=False,
    )


def _event(name="SessionStart", agent_id=None):
    return {
        "schema_version": 1,
        "session_id": "session-example",
        "agent_id": agent_id,
        "cwd": "/workspace/example",
        "model": "gpt-example",
        "hook_event_name": name,
        "turn_id": None,
    }


def _daemon(tmp_path: Path, mail: FakeAgentMail) -> BridgeDaemon:
    return BridgeDaemon(_config(tmp_path), mail)


def test_config_accepts_installer_style_absolute_codex_binary(tmp_path):
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex.chmod(0o755)
    runtime = tmp_path / "runtime"

    config = BridgeConfig.from_env(
        {
            "AGENTSTACK_CODEX_APP_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_PROJECT_KEY": "/workspace/example",
            "AGENTSTACK_MCP_URL": "http://agent-mail.invalid/api/",
            "AGENTSTACK_CODEX_BINARY": str(codex),
            "AGENTSTACK_CODEX_APP_PLUGIN_ID": (
                "agentstack-codex-app@private-market"
            ),
            "AGENTSTACK_CODEX_APP_SKIP_GIT_CHECK": "1",
            "AGENTSTACK_CODEX_APP_STALE_AFTER_SECONDS": "7200",
            "AGENTSTACK_CODEX_APP_RETRY_MAX_ATTEMPTS": "9",
            "AGENTSTACK_CODEX_APP_RETRY_MAX_AGE_SECONDS": "1800",
            "AGENTSTACK_CODEX_APP_RETRY_MAX_BACKOFF_SECONDS": "120",
            "CODEX_HOME": str(tmp_path / ".codex"),
        }
    )

    assert config.codex_binary == str(codex)
    assert config.plugin_id == "agentstack-codex-app@private-market"
    assert config.skip_git_repo_check is True
    assert config.stale_after_seconds == 7200
    assert config.registration_retry_max_attempts == 9
    assert config.registration_retry_max_age_seconds == 1800
    assert config.registration_retry_max_backoff_seconds == 120
    assert config.codex_sessions_root == tmp_path / ".codex" / "sessions"


def test_process_event_registers_once_and_reuses_stable_binding(tmp_path):
    mail = FakeAgentMail()
    daemon = _daemon(tmp_path, mail)
    external_id = daemon.process_event(_event())
    first = daemon.identities.resolve(external_id)
    token = daemon.identities.load_owner_token(external_id)

    daemon.process_event(_event("UserPromptSubmit"))
    second = daemon.identities.resolve(external_id)
    snapshot = read_snapshot(daemon.config.snapshot_path)["runtimes"][0]

    assert first["agent_name"] == second["agent_name"] == "Calm-Noether"
    assert token and token not in json.dumps(snapshot)
    assert len(mail.calls) == 1
    assert "agent_name" not in mail.calls[0]
    assert snapshot["state"] == "working"


def test_registration_response_name_drives_binding_signal_and_delivery(tmp_path):
    config = replace(
        _config(tmp_path),
        signals_dir=tmp_path / "signals",
        project_slug="example-project",
    )
    mail = FakeAgentMail(
        normalize_names=True,
        server_name="Wild-McClintock",
    )
    daemon = BridgeDaemon(config, mail)

    external_id = daemon.process_event(_event())
    binding = daemon.identities.resolve(external_id)
    assert binding["agent_name"] == "WildMcClintock"
    assert "agent_name" not in mail.calls[0]
    assert read_snapshot(config.snapshot_path)["runtimes"][0]["agent_name"] == (
        "WildMcClintock"
    )

    signal_dir = (
        config.signals_dir
        / "projects"
        / "example-project"
        / "agents"
        / "WildMcClintock"
    )
    signal_dir.mkdir(parents=True)
    (signal_dir / "7.signal").write_text(
        json.dumps(
            {
                "project": "example-project",
                "agent": "WildMcClintock",
                "message": {
                    "id": 7,
                    "from": "SteelBoltzmann",
                    "subject": "Canonical delivery",
                },
            }
        ),
        encoding="utf-8",
    )
    assert len(daemon._pending_signal_fingerprint(binding)) == 1

    delivery = DeliveryManager(tmp_path / "delivery.sqlite3")
    adapter = FakeResumeAdapter()
    coordinator = WakeCoordinator(
        delivery,
        daemon.identities,
        daemon.snapshots,
        adapter,
        signals_dir=config.signals_dir,
        project_slug=lambda _: "example-project",
        policy=WakePolicy(coalesce_seconds=0),
    )
    coordinator.tick([binding])

    assert len(adapter.calls) == 1
    assert delivery.rows()[0]["agent_name"] == "WildMcClintock"
    assert delivery.rows()[0]["status"] == "leased"


def test_worker_startup_reconciles_existing_binding_and_snapshot(tmp_path):
    config = _config(tmp_path)
    identities = IdentityStore(config.runtime_dir / "identity")
    binding = identities.save(
        build_binding(
            session_id="session-existing",
            agent_id=None,
            agent_name="White-Meitner",
            project_key="/workspace/example",
            now="2026-01-01T00:00:00Z",
        )
    )
    identities.store_owner_token(binding["external_id"], "owner-token")
    snapshots = SnapshotStore(config.snapshot_path)
    snapshots.upsert(
        runtime_record(
            binding,
            {"cwd": "/workspace/example", "model": "gpt-example"},
            state="waiting",
            last_seen_at="2026-01-01T00:00:00Z",
        )
    )
    mail = FakeAgentMail(normalize_names=True)
    daemon = BridgeDaemon(
        config,
        mail,
        identity_store=identities,
        snapshot_store=snapshots,
    )
    daemon._start_worker()
    deadline = time.time() + 2
    while (
        identities.resolve(binding["external_id"])["agent_name"]
        != "WhiteMeitner"
        and time.time() < deadline
    ):
        time.sleep(0.01)
    daemon.stop()

    assert identities.resolve(binding["external_id"])["agent_name"] == "WhiteMeitner"
    runtime = read_snapshot(config.snapshot_path)["runtimes"][0]
    assert runtime["agent_name"] == "WhiteMeitner"
    assert runtime["external_id"] == binding["external_id"]
    assert runtime["last_seen_at"] == "2026-01-01T00:00:00Z"
    assert mail.calls[0]["agent_name"] == "White-Meitner"


def test_fresh_registration_adopts_server_canonical_name(tmp_path):
    daemon = BridgeDaemon(
        _config(tmp_path),
        FakeAgentMail(server_name="CalmNoether"),
    )
    external_id = daemon.process_event(_event())
    agent_name = daemon.identities.resolve(external_id)["agent_name"]

    assert "-" not in agent_name
    assert agent_name.isalnum()
    assert "agent_name" not in daemon.agent_mail.calls[0]


def test_subagent_binding_records_root_parent(tmp_path):
    mail = FakeAgentMail(server_name="BlueLake")
    daemon = _daemon(tmp_path, mail)
    external_id = daemon.process_event(_event("SubagentStart", "child-example"))
    binding = daemon.identities.resolve(external_id)
    assert binding["parent_external_id"] == external_id_for("session-example")
    assert binding["agent_name"] == "BlueLake"
    assert "agent_name" not in mail.calls[0]


def test_registration_failure_preserves_binding_and_marks_degraded(tmp_path):
    mail = FakeAgentMail()
    daemon = _daemon(tmp_path, mail)
    external_id = daemon.process_event(_event())
    mail.fail = True
    daemon.process_event(_event("SessionStart"))

    assert daemon.identities.resolve(external_id) is not None
    snapshot = read_snapshot(daemon.config.snapshot_path)["runtimes"][0]
    assert snapshot["state"] == "degraded"
    assert daemon.config.retry_path.exists()


def test_fresh_registration_failure_queues_only_sanitized_event(tmp_path):
    mail = FakeAgentMail()
    mail.fail = True
    daemon = _daemon(tmp_path, mail)
    external_id = daemon.process_event(_event())
    text = daemon.config.retry_path.read_text(encoding="utf-8")
    stored = json.loads(text)
    binding = daemon.identities.resolve(external_id)
    snapshot = read_snapshot(daemon.config.snapshot_path)["runtimes"][0]
    assert binding["agent_name"].startswith("Pending-")
    assert "agent_name" not in mail.calls[0]
    assert snapshot["state"] == "degraded"
    assert set(stored) == {
        "retry_schema_version",
        "event",
        "attempt_count",
        "first_failed_at",
        "next_attempt_at",
    }
    assert stored["retry_schema_version"] == 1
    assert stored["attempt_count"] == 1
    assert stored["event"] == _event()


def test_retry_keeps_provisional_name_local_and_reuses_owner_token(tmp_path):
    mail = FakeAgentMail()
    mail.fail = True
    daemon = _daemon(tmp_path, mail)
    external_id = daemon.process_event(_event("UserPromptSubmit"))
    first_call = mail.calls[0]

    mail.fail = False
    assert daemon.replay_spool(
        daemon.config.retry_path,
        enqueue_on_failure=True,
    ) == 1

    retry_call = next(
        call for call in mail.calls[1:] if "registration_token" in call
    )
    assert "agent_name" not in first_call
    assert "agent_name" not in retry_call
    assert retry_call["registration_token"] == first_call["registration_token"]
    assert daemon.identities.resolve(external_id)["agent_name"] == "Calm-Noether"
    assert read_snapshot(daemon.config.snapshot_path)["runtimes"][0]["state"] == "working"
    assert not daemon.config.retry_path.exists()


def test_daemon_ingress_drops_non_desktop_event_before_binding(tmp_path):
    config = replace(
        _config(tmp_path),
        enforce_surface_eligibility=True,
        codex_sessions_root=tmp_path / ".codex" / "sessions",
    )
    mail = FakeAgentMail()
    daemon = BridgeDaemon(config, mail)

    external_id = daemon.process_event(_event())

    assert daemon.identities.resolve(external_id) is None
    assert daemon.snapshots.get(external_id) is None
    assert mail.calls == []
    assert not config.retry_path.exists()


def test_legacy_cli_retry_is_dropped_and_cannot_reappear_after_restart(tmp_path):
    sessions_root = tmp_path / ".codex" / "sessions"
    transcript = (
        sessions_root / "2026" / "07" / "17" / "rollout-session-example.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "session-example",
                    "originator": "codex-tui",
                    "source": "cli",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config = replace(
        _config(tmp_path),
        enforce_surface_eligibility=True,
        codex_sessions_root=sessions_root,
    )
    stale_drain = config.retry_path.with_name(
        f".{config.retry_path.name}.drain-12345"
    )
    stale_drain.parent.mkdir(parents=True)
    stale_drain.write_text(json.dumps(_event()) + "\n", encoding="utf-8")

    first = BridgeDaemon(config, FakeAgentMail())
    assert first.recover_stale_drains() == (0, 0)
    second = BridgeDaemon(config, FakeAgentMail())
    assert second.recover_stale_drains() == (0, 0)

    external_id = external_id_for("session-example")
    assert first.identities.resolve(external_id) is None
    assert first.snapshots.get(external_id) is None
    assert not stale_drain.exists()
    assert not config.retry_path.exists()
    assert not list(config.runtime_dir.glob(".*.drain-*"))


def test_transient_retry_is_bounded_by_attempt_limit(tmp_path):
    config = replace(
        _config(tmp_path),
        registration_retry_max_attempts=2,
    )
    mail = FakeAgentMail()
    mail.fail = True
    daemon = BridgeDaemon(config, mail)

    daemon.process_event(_event())
    assert config.retry_path.exists()
    assert daemon.replay_spool(config.retry_path) == 1

    register_calls = [call for call in mail.calls if "registration_token" in call]
    assert len(register_calls) == 2
    assert all("agent_name" not in call for call in register_calls)
    assert not config.retry_path.exists()
    assert not list(config.runtime_dir.glob(".*.drain-*"))


def test_retry_older_than_lifetime_is_dropped_without_agent_mail_call(tmp_path):
    config = replace(
        _config(tmp_path),
        registration_retry_max_age_seconds=60,
    )
    config.retry_path.parent.mkdir(parents=True)
    config.retry_path.write_text(
        json.dumps(
            {
                "retry_schema_version": 1,
                "event": _event(),
                "attempt_count": 1,
                "first_failed_at": "2000-01-01T00:00:00Z",
                "next_attempt_at": "2000-01-01T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    mail = FakeAgentMail()
    daemon = BridgeDaemon(config, mail)

    assert daemon.replay_spool(config.retry_path) == 0
    assert mail.calls == []
    assert not config.retry_path.exists()


def test_retry_backoff_does_not_call_agent_mail_before_due_time(tmp_path):
    config = replace(
        _config(tmp_path),
        registration_retry_seconds=30,
    )
    mail = FakeAgentMail()
    mail.fail = True
    daemon = BridgeDaemon(config, mail)
    daemon.process_event(_event())
    mail.calls.clear()

    assert daemon.replay_spool(config.retry_path) == 0

    assert mail.calls == []
    assert config.retry_path.exists()
    stored = json.loads(config.retry_path.read_text(encoding="utf-8"))
    assert stored["attempt_count"] == 1


def test_retry_for_already_retired_binding_is_dropped_without_registration(
    tmp_path,
):
    config = _config(tmp_path)
    mail = FakeAgentMail()
    daemon = BridgeDaemon(config, mail)
    external_id = daemon.process_event(_event())
    mail.calls.clear()
    mail.retired_agents.add("Calm-Noether")
    daemon._append_retry(_event(), attempt_count=1)

    assert daemon.replay_spool(config.retry_path) == 0

    assert [next(iter(call)) for call in mail.calls] == ["whois"]
    assert daemon.identities.resolve(external_id) is not None
    assert not config.retry_path.exists()


def test_missing_owner_token_fails_closed_without_rotating_identity(tmp_path):
    mail = FakeAgentMail()
    daemon = _daemon(tmp_path, mail)
    external_id = daemon.process_event(_event())
    next(daemon.identities.secrets_dir.glob("*.token")).unlink()
    daemon.process_event(_event("SessionStart"))
    snapshot = read_snapshot(daemon.config.snapshot_path)["runtimes"][0]
    assert snapshot["state"] == "degraded"
    assert daemon.identities.load_owner_token(external_id) is None
    assert len(mail.calls) == 1


def test_cleanup_orphans_retires_with_owner_token_and_keeps_real_app(tmp_path):
    config = _config(tmp_path)
    identities = IdentityStore(config.runtime_dir / "identity")
    snapshots = SnapshotStore(config.snapshot_path)
    sessions_root = tmp_path / ".codex" / "sessions"
    sessions_root.mkdir(parents=True)

    orphan = identities.save(
        build_binding(
            session_id="session-orphan",
            agent_id=None,
            agent_name="CalmNoether",
            project_key="/workspace/example",
        )
    )
    identities.store_owner_token(orphan["external_id"], "orphan-token")
    snapshots.upsert(runtime_record(orphan, {}, state="waiting"))

    real = identities.save(
        build_binding(
            session_id="session-real",
            agent_id=None,
            agent_name="QuietCurie",
            project_key="/workspace/example",
        )
    )
    identities.store_owner_token(real["external_id"], "real-token")
    snapshots.upsert(runtime_record(real, {}, state="waiting"))
    transcript = (
        sessions_root
        / "2026"
        / "07"
        / "16"
        / "rollout-2026-session-real.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "session-real",
                    "originator": "Codex Desktop",
                    "source": "vscode",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    mail = FakeAgentMail()

    report = cleanup_orphan_bindings(
        identities,
        snapshots,
        mail,
        sessions_root=sessions_root,
    )

    assert [item["external_id"] for item in report.cleaned] == [
        orphan["external_id"]
    ]
    assert report.failures == ()
    assert identities.resolve(orphan["external_id"]) is None
    assert snapshots.get(orphan["external_id"]) is None
    assert identities.resolve(real["external_id"]) == real
    assert snapshots.get(real["external_id"]) is not None
    # whois carries the owner token too: a token-strict agent-mail refuses an
    # unauthenticated profile read, which would make cleanup misread the
    # binding as "not retired".
    assert mail.calls == [
        {
            "whois": {
                "project_key": "/workspace/example",
                "agent_name": "CalmNoether",
                "registration_token": "orphan-token",
            }
        },
        {
            "retire_agent": {
                "project_key": "/workspace/example",
                "agent_name": "CalmNoether",
                "registration_token": "orphan-token",
            }
        }
    ]


def test_cleanup_orphans_isolates_failure_and_continues_with_later_binding(
    tmp_path,
):
    config = _config(tmp_path)
    identities = IdentityStore(config.runtime_dir / "identity")
    snapshots = SnapshotStore(config.snapshot_path)
    failed = identities.save(
        build_binding(
            session_id="session-failed",
            agent_id=None,
            agent_name="CalmNoether",
            project_key="/workspace/example",
        )
    )
    identities.store_owner_token(failed["external_id"], "wrong-owner-token")
    snapshots.upsert(runtime_record(failed, {}, state="waiting"))
    cleaned = identities.save(
        build_binding(
            session_id="session-cleaned",
            agent_id=None,
            agent_name="QuietCurie",
            project_key="/workspace/example",
        )
    )
    identities.store_owner_token(cleaned["external_id"], "valid-owner-token")
    snapshots.upsert(runtime_record(cleaned, {}, state="waiting"))
    mail = FakeAgentMail()
    mail.retire_failures.add("CalmNoether")

    report = cleanup_orphan_bindings(
        identities,
        snapshots,
        mail,
        sessions_root=tmp_path / "missing-sessions",
    )

    assert [item["external_id"] for item in report.cleaned] == [
        cleaned["external_id"]
    ]
    assert [
        (failure.external_id, failure.error_code)
        for failure in report.failures
    ] == [(failed["external_id"], "retire_failed")]
    assert identities.resolve(failed["external_id"]) == failed
    assert identities.load_owner_token(failed["external_id"]) == (
        "wrong-owner-token"
    )
    assert snapshots.get(failed["external_id"]) is not None
    assert identities.resolve(cleaned["external_id"]) is None
    assert snapshots.get(cleaned["external_id"]) is None


def test_cleanup_orphans_purges_already_retired_legacy_token_binding(tmp_path):
    config = _config(tmp_path)
    identities = IdentityStore(config.runtime_dir / "identity")
    snapshots = SnapshotStore(config.snapshot_path)
    binding = identities.save(
        build_binding(
            session_id="session-legacy",
            agent_id=None,
            agent_name="GoldMaxwell",
            project_key="/workspace/example",
        )
    )
    identities.store_owner_token(binding["external_id"], "legacy-wrong-token")
    snapshots.upsert(runtime_record(binding, {}, state="waiting"))
    mail = FakeAgentMail()
    mail.retired_agents.add("GoldMaxwell")

    report = cleanup_orphan_bindings(
        identities,
        snapshots,
        mail,
        sessions_root=tmp_path / "missing-sessions",
    )

    assert [item["external_id"] for item in report.cleaned] == [
        binding["external_id"]
    ]
    assert report.failures == ()
    assert identities.resolve(binding["external_id"]) is None
    assert snapshots.get(binding["external_id"]) is None
    assert [next(iter(call)) for call in mail.calls] == ["whois"]


def test_private_socket_accepts_event_and_worker_writes_snapshot():
    with tempfile.TemporaryDirectory(prefix="cas-daemon-", dir=SHORT_TMP_DIR) as directory:
        config = _config(Path(directory))
        daemon = BridgeDaemon(config, FakeAgentMail())
        thread = threading.Thread(target=daemon.serve_forever)
        thread.start()
        deadline = time.time() + 2
        while not config.socket_path.exists() and time.time() < deadline:
            time.sleep(0.01)
        assert config.socket_path.exists()
        assert stat_mode(config.socket_path) == 0o600
        assert forward_event(_event(), config.socket_path, timeout=1) is True
        while not config.snapshot_path.exists() and time.time() < deadline:
            time.sleep(0.01)
        daemon.stop()
        thread.join(timeout=2)
        assert config.snapshot_path.exists()


def test_bridge_worker_ticks_cold_wake_coordinator():
    with tempfile.TemporaryDirectory(prefix="cas-wake-", dir=SHORT_TMP_DIR) as directory:
        config = replace(_config(Path(directory)), wake_poll_seconds=0.05)
        wake = FakeWakeCoordinator()
        daemon = BridgeDaemon(
            config,
            FakeAgentMail(),
            wake_coordinator=wake,
        )
        thread = threading.Thread(target=daemon.serve_forever)
        thread.start()
        deadline = time.time() + 2
        while not config.socket_path.exists() and time.time() < deadline:
            time.sleep(0.01)
        assert forward_event(_event(), config.socket_path, timeout=1) is True
        while (
            not any(tick for tick in wake.ticks)
            and time.time() < deadline
        ):
            time.sleep(0.01)
        daemon.stop()
        thread.join(timeout=2)
        assert any(
            tick[0]["external_id"] == external_id_for("session-example")
            for tick in wake.ticks
            if tick
        )


def test_post_tool_use_coalesces_pending_agent_mail_signals(tmp_path):
    config = replace(
        _config(tmp_path),
        signals_dir=tmp_path / "signals",
        project_slug="example-project",
    )
    daemon = BridgeDaemon(config, FakeAgentMail())
    daemon.process_event(_event())
    signal_dir = (
        config.signals_dir
        / "projects"
        / "example-project"
        / "agents"
        / "Calm-Noether"
    )
    signal_dir.mkdir(parents=True)
    (signal_dir / "7.signal").write_text(
        json.dumps(
            {
                "project": "example-project",
                "agent": "Calm-Noether",
                "message": {"id": 7, "subject": "private subject"},
            }
        ),
        encoding="utf-8",
    )

    server_side, hook_side = socket.socketpair()
    thread = threading.Thread(
        target=daemon._handle_connection,
        args=(server_side,),
    )
    thread.start()
    hook_side.sendall(
        json.dumps(_event("PostToolUse"), separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    response = json.loads(hook_side.recv(65536))
    hook_side.close()
    server_side.close()
    thread.join(timeout=1)
    assert response["pending"] == {
        "count": 1,
        "agent_name": "Calm-Noether",
        "project_key": "/workspace/example",
    }
    assert "private subject" not in json.dumps(response)
    assert daemon._pending_notice(_event("PostToolUse")) is None

    (signal_dir / "8.signal").write_text(
        json.dumps(
            {
                "project": "example-project",
                "agent": "Calm-Noether",
                "message": {"id": 8},
            }
        ),
        encoding="utf-8",
    )
    assert daemon._pending_notice(_event("PostToolUse"))["count"] == 2


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
