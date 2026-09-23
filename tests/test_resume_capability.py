"""One resume capability gates every dashboard resume entry point.

Issue #59 was user-visible because DECK and NETWORK offered resume before the
server had established that the historical row could actually be restored.
These tests keep the decision in the backend and require the UI to consume the
same fixed reason code instead of re-deriving readiness from category alone.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

from dashboard import server


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "dashboard" / "index.html"
AGENT = "RetiredCodex"
AGENT_ID = 59
SESSION_ID = "01a0c437-8d07-7680-a9fb-c7dfe71a4759"


def _codex_prerequisites(monkeypatch, tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": SESSION_ID, "cwd": str(project)},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    install_home = tmp_path / "install"
    bootstrap = install_home / "bin" / "agentstack-codex-bootstrap"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text("#!/bin/bash\n", encoding="utf-8")
    monkeypatch.setenv("AGENTSTACK_HOME", str(install_home))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(server, "_codex_transcript_path", lambda _name: str(rollout))
    monkeypatch.setattr(server, "_terminal_adapter", lambda: "fixture")
    monkeypatch.setattr(
        server,
        "_codex_registration",
        lambda _name: {
            "agent_id": AGENT_ID,
            "agent_name": AGENT,
            "project_key": str(project),
            "program": "codex-cli",
        },
    )
    monkeypatch.setattr(server.shutil, "which", lambda name: f"/fixture/{name}")
    return project


def _write_private(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def test_old_codex_row_without_provenance_is_not_guessed_ready(
    monkeypatch, tmp_path
):
    project = _codex_prerequisites(monkeypatch, tmp_path)
    _write_private(
        tmp_path / "runtime" / "child-agents" / f"{AGENT}.json",
        {
            "agent_id": AGENT_ID,
            "agent_name": AGENT,
            "project_key": str(project),
            "program": "codex-cli",
            "registration_token": "owner-token",
        },
    )

    assert (
        server._resume_capability(AGENT, "codex-cli", category="retired")
        == "provenance_missing"
    )


def test_codex_row_with_verified_child_provenance_can_be_ready(
    monkeypatch, tmp_path
):
    project = _codex_prerequisites(monkeypatch, tmp_path)
    _write_private(
        tmp_path / "runtime" / "child-agents" / f"{AGENT}.json",
        {
            "agent_id": AGENT_ID,
            "agent_name": AGENT,
            "project_key": str(project),
            "program": "codex-cli",
            "registration_token": "owner-token",
            "launch_origin": "child",
            "codex_mcp_profile": "orrery-only",
        },
    )
    child_home = tmp_path / "runtime" / "child-agents" / f"{AGENT}.codex-home"
    child_home.mkdir()
    monkeypatch.setattr(
        server,
        "_codex_resume_child_home",
        lambda _session, _registration: (str(child_home), "restored"),
    )

    assert server._resume_capability(AGENT, "codex-cli", category="retired") == "ready"


def test_jump_rechecks_capability_before_it_calls_resume(monkeypatch):
    monkeypatch.setattr(server, "_agent_program", lambda _name: "codex-cli")
    monkeypatch.setattr(server, "_has_session", lambda _name: False)
    monkeypatch.setattr(
        server,
        "_resume_capability",
        lambda *_args, **_kwargs: "provenance_missing",
    )

    def forbidden(_name):
        raise AssertionError("resume must not run after a failed capability check")

    monkeypatch.setattr(server, "do_resume", forbidden)
    result = server.do_jump(AGENT)

    assert result == {
        "ok": False,
        "error": server.RESUME_CAPABILITY_MESSAGES["provenance_missing"],
        "resume_capability": "provenance_missing",
    }


def test_finished_husk_is_preserved_when_resume_is_not_ready(monkeypatch):
    monkeypatch.setattr(server, "_agent_program", lambda _name: "codex-cli")
    monkeypatch.setattr(server, "_has_session", lambda _name: True)
    monkeypatch.setattr(server, "_terminal_adapter", lambda: "fixture")
    monkeypatch.setattr(
        server,
        "build_agents",
        lambda history_days=None: [{"name": AGENT, "category": "finished"}],
    )
    monkeypatch.setattr(
        server,
        "_resume_capability",
        lambda *_args, **_kwargs: "provenance_missing",
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("an unavailable resume must not kill or attach to the husk")

    monkeypatch.setattr(server.subprocess, "run", forbidden)
    monkeypatch.setattr(server, "_focus_existing_terminal", forbidden)
    monkeypatch.setattr(server, "_open_terminal_tmux", forbidden)
    result = server.do_jump(AGENT)

    assert result["ok"] is False
    assert result["resume_capability"] == "provenance_missing"


def test_typed_capability_error_does_not_depend_on_message_text(
    monkeypatch, tmp_path
):
    _codex_prerequisites(monkeypatch, tmp_path)
    monkeypatch.setattr(
        server,
        "_codex_resume_provenance",
        lambda _name: ({"launch_origin": "child"}, None),
    )

    def fail_with_stable_code(_session, _registration):
        raise server._ResumeCapabilityError(
            "identity_mismatch", "wording may change without changing the code"
        )

    monkeypatch.setattr(server, "_codex_resume_child_home", fail_with_stable_code)

    assert (
        server._resume_capability(AGENT, "codex-cli", category="retired")
        == "identity_mismatch"
    )


def test_row_capability_cache_deduplicates_independent_view_polls(monkeypatch):
    calls = []
    server._RESUME_CAPABILITY_CACHE.clear()
    now = [100.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])

    def capability(name, program, *, category):
        calls.append((name, program, category))
        return "ready"

    monkeypatch.setattr(server, "_resume_capability", capability)
    first = server._resume_capability_for_row(
        AGENT, "codex-cli", category="retired"
    )
    second = server._resume_capability_for_row(
        AGENT, "codex-cli", category="retired"
    )
    now[0] += server._RESUME_CAPABILITY_CACHE_TTL + 0.1
    third = server._resume_capability_for_row(
        AGENT, "codex-cli", category="retired"
    )

    assert first == second == third == "ready"
    assert calls == [
        (AGENT, "codex-cli", "retired"),
        (AGENT, "codex-cli", "retired"),
    ]


def _patch_roster(monkeypatch):
    monkeypatch.setattr(
        server,
        "tmux_state",
        lambda: {
            AGENT: {
                "name": AGENT,
                "created": 100,
                "session_id": "$59",
                "activity": 200,
                "attached": False,
                "client_tty": None,
                "cmd": "zsh",
                "pane_pid": 5900,
                "title": "",
            }
        },
    )
    monkeypatch.setattr(
        server,
        "agentmail_state",
        lambda: (
            {
                AGENT: {
                    "model": "GPT 5.6",
                    "model_raw": "gpt-5.6-sol",
                    "program": "codex-cli",
                    "task": "test",
                    "last_active": 150,
                }
            },
            {},
        ),
    )
    monkeypatch.setattr(
        server,
        "_process_tree_snapshot",
        lambda: ({5900: "zsh"}, {5900: []}),
    )
    monkeypatch.setattr(server, "_codex_app_runtimes", lambda: {})
    monkeypatch.setattr(server, "_deliverables_index", lambda: {})
    monkeypatch.setattr(server, "_retired_names", lambda _project: set())
    monkeypatch.setattr(server, "_name_substitutions", lambda: {})
    monkeypatch.setattr(server, "_agent_runtime", lambda *_args: {})
    monkeypatch.setattr(server, "_project_key", lambda: "")
    monkeypatch.setattr(server, "_codex_history_binding", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(server, "_persistent_profiles", lambda: {})


def test_deck_and_network_rows_use_the_backend_capability(monkeypatch):
    _patch_roster(monkeypatch)
    server._RESUME_CAPABILITY_CACHE.clear()
    calls = []

    def capability(name, program, *, category):
        calls.append((name, program, category))
        return "provenance_missing"

    monkeypatch.setattr(server, "_resume_capability", capability)
    deck = server.build_agents()[0]
    assert deck["resume_capability"] == "provenance_missing"

    monkeypatch.setattr(
        server,
        "_raw_graph",
        lambda: {
            "nodes": [
                {
                    "name": AGENT,
                    "program": "codex-cli",
                    "model": "gpt-5.6-sol",
                    "last_active": 200,
                    "retired": False,
                }
            ],
            "edges": [],
            "spawn": [],
        },
    )
    monkeypatch.setattr(server, "_annotations", lambda: {})
    network = server.graph_payload(4, True)["nodes"][0]
    assert network["resume_capability"] == "provenance_missing"
    assert calls == [(AGENT, "codex-cli", "finished")]


def test_network_bulk_resume_selects_only_backend_ready_rows():
    html = INDEX.read_text(encoding="utf-8")
    match = re.search(r"function selCategorize\(\)\{.*?\n\}", html, re.DOTALL)
    assert match
    harness = r"""
const selectedSet=new Set(['Ready','Old']);
const gmap=new Map([
  ['Ready',{retired:true,resumeCapability:'ready'}],
  ['Old',{retired:true,resumeCapability:'provenance_missing'}],
]);
process.stdout.write(JSON.stringify(selCategorize()));
"""
    result = subprocess.run(
        ["node", "-e", match.group(0) + "\n" + harness],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == {"exitable": [], "resumable": ["Ready"]}


def test_deck_and_detail_render_fixed_capability_reason():
    html = INDEX.read_text(encoding="utf-8")
    assert "RESUME_CAPABILITY_INFO" in html
    assert "a.resume_capability" in html
    assert "g.resumeCapability" in html
    assert "RESUME UNAVAILABLE" in html
