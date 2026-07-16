from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

from agentstack_codex_app.agent_mail_client import AgentMailError, Registration
from agentstack_codex_app.daemon import BridgeConfig, BridgeDaemon
from agentstack_codex_app.hook_entry import forward_event
from agentstack_codex_app.identity_store import external_id_for
from agentstack_codex_app.snapshot import read_snapshot


class FakeAgentMail:
    def __init__(self):
        self.calls = []
        self.fail = False

    def register_agent(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise AgentMailError("offline")
        return Registration(kwargs.get("agent_name") or "Calm-Noether", kwargs["registration_token"])


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
    return BridgeDaemon(
        _config(tmp_path),
        mail,
        name_factory=lambda: "Calm-Noether",
    )


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
    assert snapshot["state"] == "working"


def test_subagent_binding_records_root_parent(tmp_path):
    daemon = _daemon(tmp_path, FakeAgentMail())
    external_id = daemon.process_event(_event("SubagentStart", "child-example"))
    binding = daemon.identities.resolve(external_id)
    assert binding["parent_external_id"] == external_id_for("session-example")


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
    assert binding["agent_name"] == "Calm-Noether"
    assert snapshot["state"] == "degraded"
    assert set(stored) == {
        "schema_version",
        "session_id",
        "agent_id",
        "cwd",
        "model",
        "hook_event_name",
        "turn_id",
    }


def test_retry_reuses_fresh_binding_name_and_owner_token(tmp_path):
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

    assert mail.calls[1]["agent_name"] == first_call["agent_name"]
    assert mail.calls[1]["registration_token"] == first_call["registration_token"]
    assert daemon.identities.resolve(external_id)["agent_name"] == "Calm-Noether"
    assert read_snapshot(daemon.config.snapshot_path)["runtimes"][0]["state"] == "working"
    assert not daemon.config.retry_path.exists()


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


def test_private_socket_accepts_event_and_worker_writes_snapshot():
    with tempfile.TemporaryDirectory(prefix="cas-daemon-", dir="/private/tmp") as directory:
        config = _config(Path(directory))
        daemon = BridgeDaemon(
            config,
            FakeAgentMail(),
            name_factory=lambda: "Calm-Noether",
        )
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


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
