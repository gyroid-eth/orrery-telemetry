"""classify(): which tmux sessions count as a running agent."""

from __future__ import annotations

import dashboard.server as server


def test_version_string_command_is_a_running_claude_agent():
    # Claude Code 2.1.26x reports its native binary as "2.1.259" in
    # pane_current_command (measured 2026-09-05). An idle REPL has no spinner
    # glyph in the title, so without this rule it was classified "finished".
    assert server.classify("QuietBohr", "2.1.259", "Claude Code", True, "claude-code") == "agent"
    assert server.classify("QuietBohr", "2.1.259", "Claude Code", False, None) == "agent"


def test_registered_claude_session_with_shell_command_is_still_an_agent():
    # PR #6: a live tmux session registered as claude-code counts as running
    # even when pane_current_command is a shell (same rule Codex already had).
    assert server.classify("QuietBohr", "zsh", "Obsidian", True, "claude-code") == "agent"


def test_unregistered_shell_session_is_idle_and_stale_registration_is_finished():
    assert server.classify("scratch", "zsh", "shell", False, None) == "idle"
    assert server.classify("OldChild", "zsh", "shell", True, "codex-cli") == "agent"
    assert server.classify("OldChild", "zsh", "shell", True, "gemini") == "finished"
