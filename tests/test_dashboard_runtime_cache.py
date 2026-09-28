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
    # A program name stored as the model (a child re-registered by the shell
    # hook without CLAUDE_CHILD_MODEL) still names the vendor. Before this the
    # deck showed a blank LED instead of the logo (WSL2, 2026-09-07).
    assert server._provider_of("claude-code") == "anthropic"
    assert server._provider_of("codex") == "openai"
    assert server._provider_of("gpt-5.6-sol") == "openai"
    assert server._provider_of("something-else") == ""

    codex = server._parse_runtime(
        "• Opus 4.6 との比較を書いた\n"
        "gpt-5.6 xhigh · Context 46% left · ~/OSS\n"
    )
    assert codex["pane_model"] == "gpt-5.6"

    # statusline が無いペインは None（会話本文からは読まない）。登録側に任せる
    assert server._parse_runtime("running Sonnet 5 here")["pane_model"] is None


def test_pane_model_comes_only_from_a_statusline():
    # 2026-09-07 WSL2 実害: statusline 未設定の Claude Code 親（sonnet-5）が
    # 「gpt-5.6-terra の子に委任」と話しただけで Codex 表示になった。
    chatter = server._parse_runtime(
        "⏺ Spawned StellarNoether on gpt-5.6-terra (medium)\n"
        "❯ \n"
        "  ⏵⏵ auto mode on (shift+tab to cycle)\n"
    )
    assert chatter["pane_model"] is None
    assert server._pane_model_for(None, "claude-code") is None

    # Codex footer before any context is consumed still counts as a statusline
    fresh = server._parse_runtime(
        "› Ask Codex to do anything\n"
        "  gpt-5.6-terra medium · ~/work/wsl-test-project\n"
    )
    assert fresh["pane_model"] == "gpt-5.6"

    # Claude's Model: line does too
    assert server._parse_runtime("Model: Default (Sonnet 5)")["pane_model"] == "Sonnet 5"

    # A pane reading that contradicts the registered program's vendor is dropped;
    # one that agrees, or has no registered vendor to compare with, is kept.
    assert server._pane_model_for("gpt-5.6", "claude-code") is None
    assert server._pane_model_for("Sonnet 5", "codex") is None
    assert server._pane_model_for("Sonnet 5", "claude-code") == "Sonnet 5"
    assert server._pane_model_for("gpt-5.6", None) == "gpt-5.6"


def test_stale_claude_approval_outside_current_tail_is_not_ask():
    text = (
        "Do you want to run this command?\n"
        "❯ 1. Yes\n"
        + "\n".join(f"later output {i}" for i in range(20))
        + "\nAPI Error: The response stopped arriving.\n❯\n"
    )

    assert server._parse_runtime(text)["act_state"] == "wait"


def test_stale_codex_approval_outside_current_tail_is_not_ask():
    text = (
        "Press enter to confirm or esc to cancel\n"
        + "\n".join(f"later output {i}" for i in range(20))
        + "\n› Ask Codex to do anything\n"
        "gpt-5.6-terra medium · ~/work\n"
    )

    assert server._parse_runtime(text)["act_state"] == "wait"


def test_fresh_claude_approval_in_current_tail_is_ask():
    text = (
        "tool request\n"
        "Do you want to run this command?\n"
        "❯ 1. Yes\n"
        "  2. Yes, and don't ask again\n"
        "  3. No\n"
    )

    assert server._parse_runtime(text)["act_state"] == "ask"


def test_fresh_codex_approval_in_current_tail_is_ask():
    text = (
        "Would you like to run the following command?\n"
        "› Yes\n"
        "  No\n"
        "Press enter to confirm or esc to cancel\n"
    )

    assert server._parse_runtime(text)["act_state"] == "ask"
