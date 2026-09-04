"""Regression coverage for session-scoped tmux runtime attributes."""

from __future__ import annotations

from types import SimpleNamespace

import dashboard.server as server


def test_runtime_keeps_attributes_but_not_live_state_across_failed_scrape(
    monkeypatch,
):
    captures = iter([
        SimpleNamespace(
            returncode=0,
            stdout=(
                "Model: Default (Opus 5 with 1M context enabled)\n"
                "ctx: 42% used\n"
                "✽ Stewing… (7s · ↓ 1.2k tokens)\n"
            ),
        ),
        SimpleNamespace(returncode=1, stdout=""),
        SimpleNamespace(returncode=1, stdout=""),
    ])
    ticks = iter((0.0, 10.0, 20.0))
    monkeypatch.setattr(server.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(server.subprocess, "run", lambda *args, **kwargs: next(captures))
    server._rt_cache.clear()

    first = server._agent_runtime(
        "TestAgent", session_created=101, session_id="$1"
    )
    assert first == {
        "ctx_used": 42,
        "act_state": "work",
        "ctx_window": "1M",
        "work_secs": 7,
        "work_disp": "7s",
        "last_disp": None,
        "pane_model": "Opus 5",
    }

    missing = server._agent_runtime(
        "TestAgent", session_created=101, session_id="$1"
    )
    assert missing["pane_model"] == "Opus 5"
    assert missing["ctx_window"] == "1M"
    assert missing["ctx_used"] is None
    assert missing["act_state"] is None
    assert missing["work_secs"] is None
    assert missing["work_disp"] is None

    # tmux timestamps are second-resolution. session_id must still distinguish a
    # kill/recreate cycle that happens within the same second.
    restarted = server._agent_runtime(
        "TestAgent", session_created=101, session_id="$2"
    )
    assert all(value is None for value in restarted.values())

    server._prune_runtime_cache({})
    assert "TestAgent" not in server._rt_cache


def test_truncated_model_status_has_bounded_context_window_match():
    parsed = server._parse_runtime(
        "header\nModel: Default (Opus 5 with 1M con…)\nfooter"
    )

    assert parsed["pane_model"] == "Opus 5"
    assert parsed["ctx_window"] == "1M"

    prose = server._parse_runtime("Please test this code with 2M context later")
    assert prose["ctx_window"] is None


def test_fable_statusline_wins_over_model_names_in_conversation():
    # 2026-09-04 実害: Fable 親の会話中に "gpt-5.6-sol" が出ると、statusline の
    # "Fable 5.1" を読めずに gpt を実モデルと誤読し、cockpit で Codex 表示になった。
    parsed = server._parse_runtime(
        "⏺ 英語 docs は gpt-5.6-sol の子に委任した\n"
        "❯ \n"
        "  Agents & Obsidian | Fable 5.1 | ctx: 13% used\n"
    )
    assert parsed["pane_model"] == "Fable 5.1"
    assert server._provider_of(parsed["pane_model"]) == "anthropic"

    codex = server._parse_runtime(
        "• Opus 4.6 との比較を書いた\n"
        "gpt-5.6 xhigh · Context 46% left · ~/OSS\n"
    )
    assert codex["pane_model"] == "gpt-5.6"

    # statusline が無いペインは従来どおり末尾の最初の一致
    assert server._parse_runtime("running Sonnet 5 here")["pane_model"] == "Sonnet 5"
