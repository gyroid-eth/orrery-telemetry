"""The deck's LINKED badge must mean tmux/mail identity linkage."""
from __future__ import annotations

import pathlib

import dashboard.server as server


def _session(name: str, *, attached: bool) -> dict:
    return {
        "name": name,
        "created": 100,
        "session_id": "$1",
        "activity": 200,
        "attached": attached,
        "client_tty": "/dev/ttys001" if attached else None,
        "cmd": "claude",
        "title": "",
    }


def _build(monkeypatch, tmux_name: str, mail_name: str, *, attached: bool):
    monkeypatch.setattr(
        server, "tmux_state",
        lambda: {tmux_name: _session(tmux_name, attached=attached)},
    )
    monkeypatch.setattr(
        server, "agentmail_state",
        lambda: ({
            mail_name: {
                "model": "sonnet",
                "model_raw": "claude-sonnet",
                "program": "claude-code",
                "task": "",
                "last_active": 150,
            },
        }, {}),
    )
    monkeypatch.setattr(server, "_codex_app_runtimes", lambda: {})
    monkeypatch.setattr(server, "_deliverables_index", lambda: {})
    monkeypatch.setattr(server, "_agent_runtime", lambda *_args: {})
    monkeypatch.setattr(server, "_project_key", lambda: "")
    return server.build_agents()[0]


def test_attached_tmux_client_is_not_an_identity_link(monkeypatch):
    row = _build(
        monkeypatch, "Zesty-Einstein", "MossyEagle", attached=True,
    )
    assert row["attached"] is True
    assert row["mail_linked"] is False


def test_exact_tmux_and_mail_name_is_linked_even_without_a_client(monkeypatch):
    row = _build(
        monkeypatch, "MossyEagle", "MossyEagle", attached=False,
    )
    assert row["attached"] is False
    assert row["mail_linked"] is True


def test_linked_badge_uses_mail_linkage_not_tmux_attachment():
    ui = (
        pathlib.Path(__file__).resolve().parent.parent
        / "dashboard" / "index.html"
    ).read_text(encoding="utf-8")
    assert "if(a.mail_linked) chips.push" in ui
    assert "if(a.attached) chips.push" in ui
    assert 'a.attached) chips.push(`<span class="chip att">◉ LINKED' not in ui
