"""Managed instructions must fail closed instead of inventing coordination paths."""
from __future__ import annotations

import pathlib


ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_claude_and_codex_blocks_state_the_general_fail_closed_rule():
    for path in ("claude/CLAUDE.md", "codex/AGENTS.md"):
        text = _read(path)
        assert "Canonical Coordination Paths Are Fail-Closed".casefold() in text.casefold()
        assert "report the exact failure and stop" in text
        assert "Do not invent a substitute" in text
        for example in (
            "mailbox directories", "storage.sqlite3", "find", "while true",
            "direct database queries", "tmux",
        ):
            assert example in text, (path, example)


def test_delegation_is_an_example_of_the_general_rule_not_an_exception():
    claude = _read("claude/CLAUDE.md")
    codex = _read("codex/AGENTS.md")
    skill = _read("skills/delegate/SKILL.md")

    assert "Delegation is one instance" in claude
    assert "Delegation is one instance" in codex
    assert "Do not substitute Claude" in claude
    assert "direct-mode launcher" in claude
    assert "For fallback direct mode when MCP tools are unavailable" not in skill
    assert "invoke the launcher's direct mode as a" in skill
    assert "report the exact failure and stop delegation" in skill


def test_both_blocks_carry_the_operational_rules_learned_in_production():
    """Rules that only lived in one maintainer's vault until 2026-09-03.

    Each line below is a lesson with a real incident behind it (short TTLs
    expiring before the edit, a second edit blocked after the hook released
    the first, improvised polling that ate the push notification, tmux
    keystrokes that never submitted). They must reach first-time installs.
    """
    for path in (ROOT / "claude" / "CLAUDE.md", ROOT / "codex" / "AGENTS.md"):
        text = path.read_text(encoding="utf-8")
        for phrase in (
            "`ttl_seconds` must be at least 600",
            "## Messaging Other Agents",
            "Send pointers, not files",
            "`tmux send-keys`",
            "## Waiting For Replies",
            "agentstack-await-reply --agent-name",
            "## Reading Notifications",
            "Body (complete; no inbox fetch needed)",
            "Fetch inbox to read the rest",
        ):
            assert phrase in text, (path.name, phrase)
    claude = (ROOT / "claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "releases its reservation" in claude
    assert "reserving it again first" in claude
    codex = (ROOT / "codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert "Nothing releases for you" in codex


def test_notification_shapes_in_the_blocks_match_the_watcher():
    watcher = (ROOT / "hooks" / "watch_agent_mail_signals.sh").read_text(encoding="utf-8")
    for phrase in (
        "Body (complete; no inbox fetch needed)",
        "Fetch inbox to read the rest",
        "Please call fetch_inbox to read it",
    ):
        assert phrase in watcher, phrase
        assert phrase in (ROOT / "claude" / "CLAUDE.md").read_text(encoding="utf-8"), phrase
