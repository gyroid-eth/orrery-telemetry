"""End-to-end fixtures for Codex launch expectations and SessionStart receipts."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from dashboard import server


ROOT = Path(__file__).resolve().parents[1]
AGENT = "BoundCodex"
AGENT_ID = 73
SESSION_ID = "01d13f58-8e1a-7777-a3d8-e2ba243cdb49"


def _module(relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare_mod = _module("hooks/prepare-codex-session-binding.py", "prepare_codex_binding")
record_mod = _module(
    "integrations/codex_app/plugin/scripts/record-codex-session-index.py",
    "record_codex_binding",
)


def _registration(project: Path) -> dict:
    return {
        "agent_id": AGENT_ID,
        "agent_name": AGENT,
        "project_key": str(project),
        "program": "codex",
    }


def _rollout(path: Path, session_id: str = SESSION_ID) -> None:
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": str(path.parent)},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _payload(transcript: Path, **overrides: object) -> dict:
    payload = {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "session_id": SESSION_ID,
        "transcript_path": str(transcript),
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def binding_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    runtime = tmp_path / "runtime"
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "sessions" / "rollout.jsonl"
    transcript.parent.mkdir()
    _rollout(transcript)

    db = tmp_path / "mail.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT);
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                name TEXT,
                program TEXT,
                last_active_ts TEXT
            );
            """
        )
        connection.execute("INSERT INTO projects VALUES (1, ?)", (str(project),))
        connection.execute(
            "INSERT INTO agents VALUES (?, 1, ?, 'codex', '2026-09-15 08:00:00')",
            (AGENT_ID, AGENT),
        )

    monkeypatch.setattr(server, "DB_PATH", str(db))
    monkeypatch.setattr(server, "PROJECT_KEY", str(project))
    monkeypatch.setattr(server, "VAULT", "")
    monkeypatch.setattr(server, "SESSION_INDEX_DIR", str(runtime / "session_index"))
    monkeypatch.setattr(server, "CODEX_LAUNCH_DIR", str(runtime / "codex_launches"))
    return {
        "runtime": runtime,
        "project": project,
        "transcript": transcript,
        "registration": _registration(project),
    }


def _prepare(env: dict, *, now: float = 100.0, history_mode: str = "enabled"):
    return prepare_mod.prepare(
        env["runtime"],
        env["registration"],
        launch_kind="startup",
        history_mode=history_mode,
        now=now,
    )


def _record(env: dict, launch_path: Path, launch_id: str, **overrides: object) -> str:
    return record_mod.record_payload(
        _payload(env["transcript"], **overrides),
        launch_path=launch_path,
        launch_id=launch_id,
    )


def _adopt_child_handoff(
    tmp_path: Path,
    *,
    runtime: Path,
    agent_name: str,
    project_key: str,
    token: Path,
) -> Path:
    """Run spawn_child.sh's real handoff contract without starting tmux."""

    spawn = (ROOT / "hooks" / "spawn_child.sh").read_text(encoding="utf-8")
    start = spawn.index("child_token_file_path() {")
    end = spawn.index("\n# Restore the canonical token file", start)
    sidecar = token.with_name(token.name + ".binding.json")
    script = (
        f'RUNTIME_DIR="{runtime}"\n'
        'CHILD_STATE_DIR="$RUNTIME_DIR/child-agents"\n'
        + spawn[start:end]
        + f'\nadopt_child_token_file "{agent_name}" "{project_key}" '
        f'"{token}" true "{sidecar}"\n'
    )
    adopted = subprocess.run(
        ["bash", "-c", script], text=True, capture_output=True, check=False
    )
    assert adopted.returncode == 0, adopted.stderr
    state_path = runtime / "child-agents" / f"{agent_name}.json"
    assert state_path.is_file(), {
        "stdout": adopted.stdout,
        "stderr": adopted.stderr,
        "token": str(token),
        "sidecar": str(sidecar),
    }
    return state_path


def test_metadata_without_hook_becomes_unconfirmed_after_grace(binding_env) -> None:
    _prepare(binding_env, now=100.0)

    pending = server._codex_history_binding(AGENT, now=109.0)
    unconfirmed = server._codex_history_binding(AGENT, now=111.0)

    assert pending["history_binding"] == "pending"
    assert unconfirmed["history_binding"] == "unconfirmed"
    assert unconfirmed["history_binding_reason_code"] == "hook_not_observed"


def test_future_launch_timestamp_does_not_extend_the_display_grace(binding_env) -> None:
    _prepare(binding_env, now=200.0)

    state = server._codex_history_binding(AGENT, now=100.0)

    assert state["history_binding"] == "unconfirmed"
    assert state["history_binding_reason_code"] == "hook_not_observed"


def test_same_launch_payload_becomes_the_verified_receipt(binding_env) -> None:
    launch_path, launch_id = _prepare(binding_env)

    assert _record(binding_env, launch_path, launch_id) == "bound"
    state = server._codex_history_binding(AGENT, now=200.0)

    assert state["history_binding"] == "bound"
    assert state["transcript_path"] == str(binding_env["transcript"])
    receipt = json.loads(
        (binding_env["runtime"] / "session_index" / f"{AGENT_ID}.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["launch_id"] == launch_id
    assert receipt["provider"] == "codex"
    assert receipt["registered_by"] == AGENT


def test_resume_revalidates_the_same_session_id_and_payload_path(binding_env) -> None:
    launch_path, launch_id = prepare_mod.prepare(
        binding_env["runtime"],
        binding_env["registration"],
        launch_kind="resume",
        history_mode="enabled",
        now=100.0,
    )

    assert _record(
        binding_env, launch_path, launch_id, source="resume"
    ) == "bound"
    receipt = json.loads(
        (binding_env["runtime"] / "session_index" / f"{AGENT_ID}.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["launch_kind"] == "resume"
    assert receipt["source"] == "resume"
    assert receipt["session_id"] == SESSION_ID


def test_receipt_program_must_match_the_current_registration(binding_env) -> None:
    launch_path, launch_id = _prepare(binding_env)
    assert _record(binding_env, launch_path, launch_id) == "bound"
    receipt_path = binding_env["runtime"] / "session_index" / f"{AGENT_ID}.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["program"] = "codex-cli"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    state = server._codex_history_binding(AGENT, now=200.0)

    assert state["history_binding"] == "unconfirmed"
    assert state["history_binding_reason_code"] == "receipt_missing"


def test_old_valid_index_is_not_success_for_a_new_launch(binding_env) -> None:
    first_path, first_id = _prepare(binding_env, now=100.0)
    assert _record(binding_env, first_path, first_id) == "bound"

    _prepare(binding_env, now=200.0)

    assert server._codex_transcript_path(AGENT) is None
    state = server._codex_history_binding(AGENT, now=211.0)
    assert state["history_binding"] == "unconfirmed"


def test_write_failure_does_not_raise_and_is_visible(binding_env, monkeypatch) -> None:
    launch_path, launch_id = _prepare(binding_env)
    original = record_mod._atomic_json

    def fail_receipt(path: Path, payload: dict) -> None:
        if path.parent.name == "session_index":
            raise OSError("fixture: full disk")
        original(path, payload)

    monkeypatch.setattr(record_mod, "_atomic_json", fail_receipt)

    assert _record(binding_env, launch_path, launch_id) == "write_failed"
    state = server._codex_history_binding(AGENT, now=200.0)
    assert state["history_binding"] == "unconfirmed"
    assert state["history_binding_reason_code"] == "write_failed"


def test_write_failure_cannot_leave_an_old_receipt_authoritative(
    binding_env, monkeypatch
) -> None:
    launch_path, launch_id = _prepare(binding_env)
    assert _record(binding_env, launch_path, launch_id) == "bound"
    receipt_path = binding_env["runtime"] / "session_index" / f"{AGENT_ID}.json"
    old_receipt = receipt_path.read_text(encoding="utf-8")
    original = record_mod._atomic_json

    def fail_replacement(path: Path, payload: dict) -> None:
        if path == receipt_path:
            raise OSError("fixture: receipt replacement failed")
        original(path, payload)

    monkeypatch.setattr(record_mod, "_atomic_json", fail_replacement)

    assert _record(
        binding_env, launch_path, launch_id, source="compact"
    ) == "write_failed"
    assert receipt_path.read_text(encoding="utf-8") == old_receipt
    state = server._codex_history_binding(AGENT, now=200.0)
    assert state["history_binding"] == "unconfirmed"
    assert state["history_binding_reason_code"] == "write_failed"


def test_receipt_commit_is_success_even_if_diagnostic_update_fails(
    binding_env, monkeypatch
) -> None:
    launch_path, launch_id = _prepare(binding_env)
    original = record_mod._atomic_json

    def fail_bound_hint(path: Path, payload: dict) -> None:
        if path == launch_path and payload.get("last_reason") == "bound":
            raise OSError("fixture: diagnostic metadata is not writable")
        original(path, payload)

    monkeypatch.setattr(record_mod, "_atomic_json", fail_bound_hint)

    assert _record(binding_env, launch_path, launch_id) == "bound"
    assert server._codex_history_binding(AGENT, now=200.0)["history_binding"] == "bound"


def test_launch_expectation_atomic_failure_never_reports_a_new_generation(
    binding_env, monkeypatch
) -> None:
    launch_path, launch_id = _prepare(binding_env)
    assert _record(binding_env, launch_path, launch_id) == "bound"
    original = prepare_mod._atomic_json

    def fail_launch(path: Path, payload: dict) -> None:
        raise OSError("fixture: launch metadata is not writable")

    monkeypatch.setattr(prepare_mod, "_atomic_json", fail_launch)
    with pytest.raises(OSError):
        _prepare(binding_env, now=200.0)
    monkeypatch.setattr(prepare_mod, "_atomic_json", original)

    # The old run remains internally consistent; callers must abort before a
    # new CLI starts because no new generation was returned.
    assert server._codex_history_binding(AGENT, now=211.0)["history_binding"] == "bound"
    assert _record(binding_env, launch_path, launch_id) == "bound"


def test_launch_lock_open_failure_never_reports_a_new_generation(
    binding_env, monkeypatch
) -> None:
    launch_path, launch_id = _prepare(binding_env)
    assert _record(binding_env, launch_path, launch_id) == "bound"
    original_open = prepare_mod.os.open

    def fail_lock(path, *args, **kwargs):
        if str(path).endswith(".lock"):
            raise OSError("fixture: launch lock cannot be opened")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(prepare_mod.os, "open", fail_lock)
        with pytest.raises(OSError):
            _prepare(binding_env, now=200.0)

    assert server._codex_history_binding(AGENT, now=211.0)["history_binding"] == "bound"


def test_late_old_callback_cannot_replace_current_receipt(binding_env) -> None:
    launch_path, old_id = _prepare(binding_env, now=100.0)
    current_path, current_id = _prepare(binding_env, now=200.0)
    assert launch_path == current_path
    assert _record(binding_env, current_path, current_id) == "bound"

    other = binding_env["transcript"].with_name("old.jsonl")
    _rollout(other, "old-session-id")
    late = record_mod.record_payload(
        _payload(other, session_id="old-session-id"),
        launch_path=launch_path,
        launch_id=old_id,
    )

    assert late == "stale_launch"
    receipt = json.loads(
        (binding_env["runtime"] / "session_index" / f"{AGENT_ID}.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["launch_id"] == current_id
    assert receipt["session_id"] == SESSION_ID


def test_inherited_same_launch_cannot_bind_a_second_cli_process(binding_env) -> None:
    launch_path, launch_id = _prepare(binding_env)
    assert _record(binding_env, launch_path, launch_id) == "bound"
    other = binding_env["transcript"].with_name("inherited-child.jsonl")
    _rollout(other, "nested-cli-session-id")

    result = record_mod.record_payload(
        _payload(other, session_id="nested-cli-session-id", source="startup"),
        launch_path=launch_path,
        launch_id=launch_id,
    )

    assert result == "id_mismatch"
    state = server._codex_history_binding(AGENT, now=200.0)
    assert state["history_binding"] == "unconfirmed"
    assert state["history_binding_reason_code"] == "id_mismatch"


def test_conflicting_session_stays_rejected_after_receipt_is_removed(binding_env) -> None:
    launch_path, launch_id = _prepare(binding_env)
    assert _record(binding_env, launch_path, launch_id) == "bound"
    other = binding_env["transcript"].with_name("conflicting-session.jsonl")
    _rollout(other, "conflicting-session-id")
    conflict = _payload(other, session_id="conflicting-session-id", source="startup")

    assert record_mod.record_payload(
        conflict, launch_path=launch_path, launch_id=launch_id
    ) == "id_mismatch"
    assert not (
        binding_env["runtime"] / "session_index" / f"{AGENT_ID}.json"
    ).exists()
    for source in ("startup", "compact"):
        conflict["source"] = source
        assert record_mod.record_payload(
            conflict, launch_path=launch_path, launch_id=launch_id
        ) == "id_mismatch"
    assert server._codex_history_binding(AGENT, now=200.0)["history_binding"] == "unconfirmed"


def test_clear_cannot_switch_a_claimed_launch_to_another_session(binding_env) -> None:
    launch_path, launch_id = _prepare(binding_env)
    assert _record(binding_env, launch_path, launch_id) == "bound"
    other = binding_env["transcript"].with_name("clear-session.jsonl")
    _rollout(other, "clear-session-id")

    assert record_mod.record_payload(
        _payload(other, session_id="clear-session-id", source="clear"),
        launch_path=launch_path,
        launch_id=launch_id,
    ) == "id_mismatch"
    assert server._codex_history_binding(AGENT, now=200.0)["history_binding"] == "unconfirmed"
    # The conflict is terminal for this generation, even if A reports again.
    assert _record(binding_env, launch_path, launch_id, source="compact") == "id_mismatch"


def test_conflict_is_unconfirmed_even_if_the_stale_index_cannot_be_deleted(
    binding_env, monkeypatch
) -> None:
    launch_path, launch_id = _prepare(binding_env)
    assert _record(binding_env, launch_path, launch_id) == "bound"
    receipt_path = binding_env["runtime"] / "session_index" / f"{AGENT_ID}.json"
    old_receipt = receipt_path.read_text(encoding="utf-8")
    other = binding_env["transcript"].with_name("undeletable-conflict.jsonl")
    _rollout(other, "undeletable-session-id")
    original_unlink = Path.unlink

    def fail_receipt_unlink(path: Path, *args, **kwargs) -> None:
        if path == receipt_path:
            raise PermissionError("fixture: stale receipt cannot be deleted")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "unlink", fail_receipt_unlink)
        assert record_mod.record_payload(
            _payload(other, session_id="undeletable-session-id"),
            launch_path=launch_path,
            launch_id=launch_id,
        ) == "id_mismatch"

    assert receipt_path.read_text(encoding="utf-8") == old_receipt
    state = server._codex_history_binding(AGENT, now=200.0)
    assert state["history_binding"] == "unconfirmed"
    assert state["history_binding_reason_code"] == "id_mismatch"


def test_null_transcript_is_unconfirmed_not_disabled(binding_env) -> None:
    launch_path, launch_id = _prepare(binding_env)
    assert _record(binding_env, launch_path, launch_id, transcript_path=None) == "no_transcript"
    state = server._codex_history_binding(AGENT, now=200.0)
    assert state["history_binding"] == "unconfirmed"
    assert state["history_binding_reason_code"] == "no_transcript"


def test_explicit_no_history_launch_is_disabled(binding_env) -> None:
    _prepare(binding_env, history_mode="disabled")
    state = server._codex_history_binding(AGENT, now=200.0)
    assert state["history_binding"] == "disabled"


@pytest.mark.parametrize("hook_event", ["SessionStart", "SubagentStart"])
def test_builtin_subagent_event_is_ignored(binding_env, hook_event: str) -> None:
    launch_path, launch_id = _prepare(binding_env)
    result = _record(
        binding_env,
        launch_path,
        launch_id,
        hook_event_name=hook_event,
        agent_id="builtin-child",
    )
    assert result == "ignored"
    assert not (binding_env["runtime"] / "session_index" / f"{AGENT_ID}.json").exists()


def test_cli_entrypoint_is_fail_open_on_bad_payload(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(
                ROOT
                / "integrations/codex_app/plugin/scripts/record-codex-session-index.py"
            ),
        ],
        input="not json",
        text=True,
        capture_output=True,
        env={
            "AGENTSTACK_CODEX_LAUNCH_BINDING": str(tmp_path / "missing.json"),
            "AGENTSTACK_CODEX_LAUNCH_ID": "missing",
        },
        check=False,
    )
    assert result.returncode == 0


def test_cli_entrypoint_derives_runtime_from_launch_path_not_ambient_env(
    binding_env, tmp_path: Path
) -> None:
    launch_path, launch_id = _prepare(binding_env)
    poisoned_runtime = tmp_path / "parent-runtime"
    result = subprocess.run(
        [
            sys.executable,
            str(
                ROOT
                / "integrations/codex_app/plugin/scripts/record-codex-session-index.py"
            ),
        ],
        input=json.dumps(_payload(binding_env["transcript"])),
        text=True,
        capture_output=True,
        env={
            "AGENTSTACK_CODEX_LAUNCH_BINDING": str(launch_path),
            "AGENTSTACK_CODEX_LAUNCH_ID": launch_id,
            "AGENTSTACK_RUNTIME_DIR": str(poisoned_runtime),
        },
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (
        binding_env["runtime"] / "session_index" / f"{AGENT_ID}.json"
    ).is_file()
    assert not (poisoned_runtime / "session_index" / f"{AGENT_ID}.json").exists()


def test_preregister_receipt_reaches_child_state_without_name_lookup(tmp_path: Path) -> None:
    fake_lib = tmp_path / "register.sh"
    fake_lib.write_text(
        """
ags_mail_load_token() { :; }
ags_has_scientist_suffix() { return 0; }
ags_generate_registration_token() { printf 'sent-token\\n'; }
ags_mcp_call() {
  if [[ "$1" == register_agent ]]; then
    printf '{"id":73,"name":"BoundCodex","registration_token":"owner-token"}\\n'
  else
    printf '{}\\n'
  fi
}
ags_mcp_has_error() { return 1; }
ags_extract_agent_name() { python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])'; }
ags_extract_agent_id() { python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])'; }
ags_extract_registration_token() { python3 -c 'import json,sys; print(json.load(sys.stdin)["registration_token"])'; }
ags_store_registration_token() { :; }
ags_apply_contact_policy() { :; }
""",
        encoding="utf-8",
    )
    token = tmp_path / "token-BoundCodex"
    preregister = subprocess.run(
        [
            str(ROOT / "bin" / "agentstack-preregister-child"),
            "--project-key",
            str(tmp_path / "project"),
            "--name",
            AGENT,
            "--program",
            "codex",
            "--model",
            "gpt-test",
            "--token-file-out",
            str(token),
        ],
        text=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "AGENTSTACK_REGISTER_LIB": str(fake_lib),
            "AGENTSTACK_ENV_FILE": "",
        },
        check=False,
    )
    assert preregister.returncode == 0, preregister.stderr
    sidecar = token.with_name(token.name + ".binding.json")
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {
        "agent_id": AGENT_ID,
        "agent_name": AGENT,
        "project_key": str(tmp_path / "project"),
        "program": "codex",
    }

    runtime = tmp_path / "runtime"
    state_path = _adopt_child_handoff(
        tmp_path,
        runtime=runtime,
        agent_name=AGENT,
        project_key=str(tmp_path / "project"),
        token=token,
    )
    state = json.loads(
        state_path.read_text(encoding="utf-8")
    )
    assert state["agent_id"] == AGENT_ID
    assert state["program"] == "codex"
    assert not token.exists() and not sidecar.exists()

    launch_path, launch_id = prepare_mod.prepare(
        runtime, state, launch_kind="startup", history_mode="enabled", now=100.0
    )
    transcript = tmp_path / "delegate-rollout.jsonl"
    _rollout(transcript)
    assert record_mod.record_payload(
        _payload(transcript), launch_path=launch_path, launch_id=launch_id
    ) == "bound"
    assert (runtime / "session_index" / f"{AGENT_ID}.json").is_file()


def test_deck_new_agent_handoff_reaches_recorder_and_reader(
    binding_env, monkeypatch, tmp_path: Path
) -> None:
    runtime = binding_env["runtime"]
    runtime.mkdir()
    (runtime / "agent_token_Parent").write_text(
        "parent-owner-token", encoding="utf-8"
    )
    launcher = tmp_path / "spawn_child.sh"
    launcher.write_text("#!/bin/bash\n", encoding="utf-8")
    launcher.chmod(0o755)
    launched: list[list[str]] = []

    def mcp(method: str, _args: dict, timeout: int = 15) -> dict:
        del timeout
        if method == "register_agent":
            return {
                "ok": True,
                "data": {
                    "id": AGENT_ID,
                    "name": AGENT,
                    "registration_token": "server-child-token",
                },
            }
        return {"ok": True, "data": {}}

    monkeypatch.setattr(server, "SPAWN_SCRIPT", str(launcher))
    monkeypatch.setattr(server, "HERE", str(tmp_path))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "_spawn_name_status", lambda _name: "available")
    monkeypatch.setattr(server, "_mcp_call", mcp)
    monkeypatch.setattr(server, "_runtime_agent_token", lambda _name: "parent-owner-token")
    with monkeypatch.context() as process_patch:
        process_patch.setattr(
            server.subprocess, "Popen", lambda args, **_kwargs: launched.append(args)
        )
        process_patch.setattr(
            server.subprocess,
            "run",
            lambda *_args, **_kwargs: type("Result", (), {"returncode": 0})(),
        )
        result = server.do_spawn(
            {
                "parent": "Parent",
                "name": AGENT,
                "task": "verify deck binding",
                "dir": str(tmp_path),
                "provider": "codex",
                "model": "gpt-5.6-sol",
                "effort": "high",
            }
        )
    assert result["ok"] is True, result

    token = Path(launched[0][4])
    state_path = _adopt_child_handoff(
        tmp_path,
        runtime=runtime,
        agent_name=AGENT,
        project_key=str(binding_env["project"]),
        token=token,
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["agent_id"] == AGENT_ID
    assert state["program"] == "codex-cli"
    with sqlite3.connect(server.DB_PATH) as connection:
        connection.execute(
            "UPDATE agents SET program='codex-cli' WHERE id=?", (AGENT_ID,)
        )

    launch_path, launch_id = prepare_mod.prepare(
        runtime, state, launch_kind="startup", history_mode="enabled", now=100.0
    )
    assert _record(binding_env, launch_path, launch_id) == "bound"
    assert server._codex_history_binding(AGENT, now=200.0)["history_binding"] == "bound"


def test_deck_badges_are_only_wired_in_the_card_renderer() -> None:
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    card_start = html.index("function bay(a,i)")
    card_end = html.index("\nfunction render()", card_start)
    card = html[card_start:card_end]
    assert "? UNBOUND" in card
    assert "— NO HISTORY" in card
    assert html.count("? UNBOUND") == 1
    assert html.count("— NO HISTORY") == 1
