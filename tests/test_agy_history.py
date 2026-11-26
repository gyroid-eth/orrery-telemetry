"""Antigravity Reader receipts and phone projection on private fixtures."""
import json
import time

import pytest

from dashboard import agy_history, mobile_history


SID = "12345678-1234-4321-8421-123456789abc"
OTHER_SID = "12345678-1234-4321-8421-123456789abd"
NAME = "FixtureAgent"
REGISTRATION = dict(agent_id=5, project_key="fixture", program="antigravity")


@pytest.fixture
def launch(tmp_path):
    root = tmp_path.resolve()
    runtime, home, workspace = (root / key for key in ("runtime", "home", "work"))
    generation = dict(session_id="$3", created=int(time.time()) - 1, tmux_pid=42)
    proof = "|".join([NAME, *(str(generation[key]) for key in ("session_id", "created", "tmux_pid"))])
    nonce = agy_history.prepare(runtime, home, NAME, 5, "fixture", workspace, proof)
    transcript = home / ".gemini/antigravity-cli/brain" / SID / ".system_generated/logs/transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    payload = dict(conversationId=SID, transcriptPath=str(transcript), workspacePaths=[str(workspace)])
    return runtime, generation, proof, nonce, transcript, payload


def test_exact_receipt_binds_id_path_and_registration(launch):
    runtime, generation, _, nonce, transcript, payload = launch
    assert agy_history.binding(runtime, NAME, REGISTRATION, generation)["history_binding"] == "unbound"
    agy_history.record(runtime, NAME, nonce, payload)
    bound = agy_history.binding(runtime, NAME, REGISTRATION, generation)
    assert (bound["history_binding"], bound["history_session_id"], bound["transcript_path"]) == (
        "bound", SID, str(transcript))
    assert bound["history_workspace"] == payload["workspacePaths"][0]
    agy_history.record(runtime, NAME, nonce, payload)
    assert agy_history.binding(runtime, NAME, REGISTRATION, generation) == bound
    assert agy_history.binding(runtime, NAME, dict(REGISTRATION, agent_id=6), generation)["transcript_path"] is None
    assert agy_history.binding(runtime, NAME, REGISTRATION, dict(generation, tmux_pid=13))["history_binding"] == "stale"


def test_accepts_the_full_transcript_path_issued_by_agy(launch):
    runtime, generation, _, nonce, transcript, payload = launch
    full = transcript.with_name("transcript_full.jsonl")
    transcript.rename(full)
    agy_history.record(runtime, NAME, nonce, dict(payload, transcriptPath=str(full)))
    assert agy_history.binding(runtime, NAME, REGISTRATION, generation)["transcript_path"] == str(full)


def test_old_or_unrelated_callbacks_never_select_a_transcript(launch):
    runtime, generation, _, nonce, transcript, payload = launch
    agy_history.record(runtime, NAME, "wrong", payload)
    agy_history.record(runtime, NAME, nonce, dict(payload, workspacePaths=[]))
    with pytest.raises(ValueError, match="unexpected transcript"):
        agy_history.record(runtime, NAME, nonce, dict(payload, conversationId=OTHER_SID))
    assert agy_history.binding(runtime, NAME, REGISTRATION, generation)["transcript_path"] is None
    agy_history.record(runtime, NAME, nonce, payload)
    agy_history.record(runtime, NAME, nonce, dict(payload, conversationId=OTHER_SID,
                        transcriptPath=str(transcript).replace(SID, OTHER_SID)))
    assert agy_history.binding(runtime, NAME, REGISTRATION, generation)["history_binding_reason_code"] == "session_conflict"


def test_new_launch_invalidates_the_old_receipt(launch):
    runtime, generation, proof, nonce, transcript, payload = launch
    agy_history.record(runtime, NAME, nonce, payload)
    home = transcript.parents[6]
    fresh = agy_history.prepare(runtime, home, NAME, 5, "fixture", payload["workspacePaths"][0], proof)
    assert fresh != nonce
    agy_history.record(runtime, NAME, nonce, payload)
    result = agy_history.binding(runtime, NAME, REGISTRATION, generation)
    assert result["history_binding"] == "unbound" and result["transcript_path"] is None


def test_dashboard_binding_rejects_a_symlink_and_protected_mode(monkeypatch, launch):
    from dashboard import server
    runtime, generation, _, nonce, transcript, payload = launch
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "GLOBAL_MODE", False)
    monkeypatch.setattr(server, "FRESH_MODE", False)
    monkeypatch.setattr(server, "_resume_registration", lambda name, programs: REGISTRATION if
                       name == NAME and "antigravity" in programs else None)
    row = dict(program="antigravity", name=NAME, running=True)
    agy_history.record(runtime, NAME, nonce, payload)
    def binding():
        return server._history_binding_fields(row, now=0, session_state=generation)
    assert binding()["history_binding"] == "bound"
    transcript.rename(transcript.with_name("moved.jsonl"))
    transcript.symlink_to("moved.jsonl")
    assert binding()["transcript_path"] is None
    monkeypatch.setattr(server, "FRESH_MODE", True)
    assert binding()["history_binding_reason_code"] == "protected_history_required"


def test_provider_setup_preserves_user_groups(tmp_path):
    hooks = tmp_path.resolve() / "hooks.json"
    custom = {"personal": {"Stop": [{"type": "command", "command": "echo personal"}]}}
    hooks.write_text(json.dumps(custom))
    agy_history.configure(hooks, "install")
    updated = json.loads(hooks.read_text())
    assert updated["personal"] == custom["personal"]
    assert set(updated["orrery-reader"]) == {"SessionStart", "PreInvocation"}
    saved = hooks.stat().st_mtime_ns
    agy_history.configure(hooks, "install")
    assert hooks.stat().st_mtime_ns == saved
    agy_history.configure(hooks, "uninstall")
    assert json.loads(hooks.read_text()) == custom
    hooks.write_text(json.dumps(dict(custom, **{"orrery-reader": {"enabled": False}})))
    original = hooks.read_bytes()
    with pytest.raises(ValueError, match="differs from the managed group"):
        agy_history.configure(hooks, "install")
    assert hooks.read_bytes() == original


def test_phone_keeps_provider_result_and_compact_label_without_payload(monkeypatch):
    client = mobile_history.HistoryClient(1)
    monkeypatch.setattr(client, "_get", lambda url: (200, {
        "ok": True, "provider": "antigravity", "mode": "cursor",
        "events": [
            dict(id="1", kind="tool_result", role="assistant", text="HIDDEN", references=[{"label": "HIDDEN"}]),
            dict(id="2", kind="system", role="system", text="Context compacted", system_detail="HIDDEN"),
        ],
    }))
    page = client.page(NAME)
    assert page["provider"] == "antigravity"
    assert [(row["kind"], row["text"]) for row in page["events"]] == [
        ("tool", "Tool result"), ("tool", "Context compacted")]
    assert "HIDDEN" not in json.dumps(page)
