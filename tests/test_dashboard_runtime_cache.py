"""Regression coverage for session-scoped tmux runtime attributes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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
    monkeypatch.setattr(server, "_tmux_run", lambda *args, **kwargs: next(captures))
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


_CLAUDE_INPUT = (
    "────────────────────────────────────────\n"
    "❯ \n"
    "────────────────────────────────────────\n"
    "fixture | Sonnet 5 | ctx: 33% used\n"
)
_CODEX_INPUT = "› Ask Codex to do anything\ngpt-5.6-terra medium · Context 70% left\n"
_OLD_WORK = "✽ Stewing… (18s · ↓ 1.2k tokens)\n"
_CLAUDE_ASK = "Do you want to run this command?\n❯ 1. Yes\n  2. No\n"
_CODEX_ASK = "Would you like to run this command?\n› Yes\n  No\nPress enter to confirm or esc to cancel\n"
_QUESTION = "Choose a result\n❯ 1. Option\nEnter to select · Esc to cancel\n"


@pytest.mark.parametrize("history,current,state", [
    (_OLD_WORK + "\n⏺ Finished the requested work.\n\n", _CLAUDE_INPUT, "wait"),
    (_OLD_WORK + "\n✻ Worked for 6s · done 6:39 PM\n", _CLAUDE_INPUT, "wait"),
    ("• Working (18s • esc to interrupt)\n\n• Finished.\n\n", _CODEX_INPUT, "wait"),
    ("The example mentions esc to interrupt and · 1.2k tokens.\n\n", _CLAUDE_INPUT, "wait"),
    ("```text\n" + _OLD_WORK + "```\n", _CLAUDE_INPUT, "wait"),
    (_OLD_WORK + "\n", _CLAUDE_ASK, "ask"),
    (_OLD_WORK + "\n", "\n".join("  " + row for row in _CLAUDE_ASK.splitlines()), "ask"),
    (_OLD_WORK + "\n", _CODEX_ASK, "ask"),
    (_OLD_WORK + "\n", _QUESTION, "question"),
    (_CLAUDE_ASK + "\n⏺ Permission was granted; the task is complete.\n", _CLAUDE_INPUT, "wait"),
    (_QUESTION + "\n⏺ Your answer was recorded.\n", _CLAUDE_INPUT, "wait"),
])
def test_current_terminal_activity_ignores_history(history, current, state):
    parsed = server._parse_runtime(history + current)
    assert parsed["act_state"] == state
    assert parsed["work_secs"] is None
    assert parsed["work_disp"] is None


@pytest.mark.parametrize("current,footer,seconds", [
    ("✳ Stewing… (7s · ↓ 1.1k tokens)\n", _CLAUDE_INPUT, 7),
    ("✽ Inspecting files…\n", _CLAUDE_INPUT, None),
    ("✽\n Inspecting files…\n", _CLAUDE_INPUT, None),
    ("✽ Inspecting several\n wrapped task labels…\n", _CLAUDE_INPUT, None),
    ("✽ Inspecting several\n wrapped task\n labels…\n", _CLAUDE_INPUT, None),
    ("✽\n", _CLAUDE_INPUT, None),
    ("• Working (2s • esc to interrupt)\n", _CODEX_INPUT, 2),
    ("▌ Thinking (2s • esc to interrupt)\n", _CODEX_INPUT, 2),
    ("● Working (2s • esc to interrupt)\n", _CODEX_INPUT, 2),
    ("• Starting MCP servers (2/3): fixture (0s • esc to interrupt)\n", _CODEX_INPUT, 0),
])
def test_current_terminal_activity_keeps_work_with_an_input_prompt(current, footer, seconds):
    # Old completed controls and an old elapsed time must not win either way.
    parsed = server._parse_runtime(_OLD_WORK + _CLAUDE_ASK + _QUESTION + "\n" + current + footer)
    assert parsed["act_state"] == "work"
    assert parsed["work_secs"] == seconds
    assert parsed["work_disp"] == (None if seconds is None else f"{seconds}s")


# Transcribed from the operator-supplied hook review screenshot. This is not
# the repository's older folder-access trust fixture.
_HOOK_REVIEW = (
    "Hooks need review\n"
    "1 hook is new or changed.\n"
    "Hooks can run outside the sandbox after you trust them.\n\n"
    "› 1. Review hooks\n"
    "  2. Trust all and continue\n"
    "  3. Continue without trusting (hooks won't run)\n\n"
    "enter confirm · esc skip\n"
)


@pytest.mark.parametrize("history", ["", _OLD_WORK + "\n"])
def test_current_hook_review_is_approval(history):
    parsed = server._parse_runtime(history + _HOOK_REVIEW)
    assert parsed["act_state"] == "ask"
    assert parsed["work_disp"] is None


@pytest.mark.parametrize("text", [
    "The Hooks need review screen offers Review hooks or Trust all and continue.\n",
    "```text\n" + _HOOK_REVIEW + "```\n" + _CLAUDE_INPUT,
    _HOOK_REVIEW.replace("  3. Continue without trusting (hooks won't run)\n", ""),
    _HOOK_REVIEW.replace("enter confirm · esc skip", ""),
])
def test_quoted_or_incomplete_hook_review_is_not_approval(text):
    assert server._parse_runtime(text)["act_state"] == "wait"


def test_current_work_after_hook_review_remains_work():
    parsed = server._parse_runtime(_HOOK_REVIEW + "\n• Working (3s • esc to interrupt)\n" + _CODEX_INPUT)
    assert parsed["act_state"] == "work"
    assert parsed["work_disp"] == "3s"


@pytest.mark.parametrize("prefix", ["", "  "])
def test_reply_after_old_work_is_not_a_wrapped_task_label(prefix):
    parsed = server._parse_runtime(
        _OLD_WORK + prefix + "⏺ The completed reply contains an ellipsis…\n" + _CLAUDE_INPUT
    )
    assert parsed["act_state"] == "wait"
    assert parsed["work_disp"] is None


def test_current_work_above_an_indented_task_widget_is_retained():
    # The existing parser documents a 25-line work window for task widgets.
    parsed = server._parse_runtime(
        _OLD_WORK + "\n✽ Inspecting files…\n"
        "  Current tasks\n    first task\n    second task\n    third task\n"
        + _CLAUDE_INPUT
    )
    assert parsed["act_state"] == "work"
    assert parsed["work_disp"] is None


def test_newest_indented_work_label_owns_the_elapsed_time():
    parsed = server._parse_runtime(
        _OLD_WORK + "\n  ✳ Stewing… (7s · ↓ 1.1k tokens)\n" + _CLAUDE_INPUT
    )
    assert parsed["act_state"] == "work"
    assert parsed["work_disp"] == "7s"


def test_question_footer_before_numbered_options_remains_question():
    # Preserve the existing literal in test_runtime_tmux_target.py.
    parsed = server._parse_runtime("Enter to select · Esc to cancel\n❯ 1. Option")
    assert parsed["act_state"] == "question"
    assert parsed["work_disp"] is None


_CLAUDE_EDIT_APPROVAL = (
    "Edit file\nfixture.json\n"
    "────────────────────────────────────────\n"
    "Do you want to make this edit to fixture.json?\n"
    "❯ 1. Yes\n"
    "  2. Yes, and allow Claude to edit files in its folder for this session\n"
    "  3. No\n\n"
    "Esc to cancel · Tab to amend\n"
)


@pytest.mark.parametrize("history", ["", _OLD_WORK + "\n"])
@pytest.mark.parametrize("footer", ["Esc to cancel", "Esc to cancel · Tab to amend"])
def test_current_claude_edit_approval_with_cancel_footer_is_ask(history, footer):
    text = history + _CLAUDE_EDIT_APPROVAL.replace("Esc to cancel · Tab to amend", footer)
    parsed = server._parse_runtime(text)
    assert parsed["act_state"] == "ask"
    assert parsed["work_secs"] is None
    assert parsed["work_disp"] is None


@pytest.mark.parametrize("text", [
    "Esc to cancel · Tab to amend\n",
    "❯ 1. Yes\n2. No\nEsc to cancel · Tab to amend\n",
    _CLAUDE_EDIT_APPROVAL + "The tool finished.\n",
    _CLAUDE_EDIT_APPROVAL.replace("Esc to cancel · Tab to amend", "Please use Esc to cancel · Tab to amend"),
    "```text\n" + _CLAUDE_EDIT_APPROVAL + "```\n" + _CLAUDE_INPUT,
    _CLAUDE_EDIT_APPROVAL + _CLAUDE_INPUT,
])
def test_claude_edit_approval_footer_does_not_promote_old_or_incomplete_ui(text):
    assert server._parse_runtime(text)["act_state"] == "wait"


def test_current_work_after_claude_edit_approval_remains_work():
    parsed = server._parse_runtime(
        _CLAUDE_EDIT_APPROVAL + "\n✳ Stewing… (7s · ↓ 1.1k tokens)\n" + _CLAUDE_INPUT
    )
    assert parsed["act_state"] == "work"
    assert parsed["work_disp"] == "7s"
