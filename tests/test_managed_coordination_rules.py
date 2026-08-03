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
