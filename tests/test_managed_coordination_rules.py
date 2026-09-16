"""Managed instructions must fail closed instead of inventing coordination paths."""
from __future__ import annotations

import pathlib
import subprocess

from service_teardown import TEST_LABEL_PREFIX


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


def test_delegate_skill_reads_bare_model_words_as_models_not_names():
    """2026-09-07: a WSL parent given `/delegate codex terra <task>` registered a
    child literally named "terra" on gpt-5.5, because the skill had no argument
    grammar and its Codex example still said gpt-5.5. The shorthand table and the
    "never a name" rule must ship with the skill."""
    skill = _read("skills/delegate/SKILL.md")
    assert "### How to read the arguments" in skill
    for word in ("`sol`, `terra`, `luna`, `astra`", "gpt-5.6-terra", "gpt-6-astra"):
        assert word in skill
    assert "A word such as `terra` is a model, not a name." in skill
    assert "The child's name is never taken from the arguments." in skill
    assert "gpt-5.5" not in skill


def test_codex_coordination_routes_are_ordered_and_do_not_force_raw_recovery():
    codex = _read("codex/AGENTS.md")
    headings = (
        "### 1. Embedded canonical task",
        "### 2. Bound ORRERY proxy",
        "### 3. Raw/direct connection after shell registration",
        "### 4. Recovering an existing identity over raw/direct MCP",
        "### 5. Registering a genuinely new raw/direct session",
        "### Shared boundaries",
    )
    offsets = [codex.index(heading) for heading in headings]
    assert offsets == sorted(offsets)
    assert "Use only the first matching route below" in codex
    assert "standalone launches can also use embedded-task semantics" in codex
    assert "Always try the token-safe helper first" not in codex
    assert "If you were launched with `agent-start-codex`" not in codex
    assert "Use MCP fallback" not in codex
    assert "do not fall back from a proxy failure to raw registration" in codex
    assert "It proves only the local" in codex
    assert "proxy binding, not ORRERY Mail reachability" in codex
    assert "A generic `register_agent failed` does not by itself prove" in codex
    assert "set `AGENT_NAME` to that exact existing name" in codex


def test_claude_coordination_routes_are_ordered_and_keep_hook_contract():
    claude = _read("claude/CLAUDE.md")
    spawn = _read("hooks/spawn_child.sh")
    headings = (
        "### 1. Embedded canonical task",
        "### 2. Bound ORRERY proxy",
        "### 3. Raw/direct connection after shell registration",
        "### 4. Recovering an existing identity over raw/direct MCP",
        "### 5. Registering a genuinely new raw/direct session",
        "### Shared boundaries",
        "### Claude Code hook boundary",
    )
    offsets = [claude.index(heading) for heading in headings]
    assert offsets == sorted(offsets)
    assert "Use only the first matching route below" in claude
    assert "standalone launches can also use embedded-task semantics" in claude
    assert "First calls:" not in claude
    assert "If you were launched with `agent-start`" not in claude
    assert "do not fall back from a proxy failure to raw registration" in claude
    assert "It proves only the local" in claude
    assert "proxy binding, not ORRERY Mail reachability" in claude
    assert "A generic `register_agent failed` does not by itself prove" in claude
    assert "set `AGENT_NAME` to that exact existing name" in claude
    assert 'program="claude-code"' in claude
    assert "proxy use alone does not prove that a flag exists" in claude
    assert "Sessions with a resolved `AGENT_NAME` are separately exempt" in claude
    assert spawn.count("Follow the child-agent startup procedure in CLAUDE.md") == 2
    assert "A normal delegated child that was not given a separate" in claude
    assert "must fetch only its own inbox through the selected connection" in claude
    assert "whether embedded or standalone, takes" in claude
    assert "Every successful `Edit`/`Write` releases its reservation" in claude
    assert "reserving it again first" in claude


def test_claude_setup_renders_routes_and_reservations_once_in_an_isolated_home(
    tmp_path: pathlib.Path,
):
    home = tmp_path / "home"
    claude_home = home / ".claude"
    project = tmp_path / "project"
    claude_home.mkdir(parents=True)
    project.mkdir()
    target = project / "CLAUDE.md"
    target.write_text("# Personal project instructions\n", encoding="utf-8")
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        "CLAUDE_HOME": str(claude_home),
        "AGENTSTACK_HOME": str(tmp_path / "installed-agentstack"),
        "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
        "AGENTSTACK_TEMPLATE_HOME": str(ROOT),
        "AGENTSTACK_PROJECT_KEY": str(project),
        "AGENTSTACK_CLAUDE_MD_SCOPE": "project",
    }

    for _ in range(2):
        result = subprocess.run(
            ["/bin/bash", str(ROOT / "bin" / "agentstack-claude-setup")],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "installed managed block into" in result.stdout

    generated = target.read_text(encoding="utf-8")
    assert generated.count("<!-- >>> claude-agent-stack") == 1
    assert generated.count("<!-- <<< claude-agent-stack -->") == 1
    assert generated.count("# Personal project instructions") == 1
    assert "### 1. Embedded canonical task" in generated
    assert "### 2. Bound ORRERY proxy" in generated
    assert "Bound proxy: follow its actual schema and use `reserve_files`" in generated
    assert "Renew with `renew_reservations`" in generated
    assert "release with `release_reservations`" in generated
    assert "Raw/direct MCP: acquire with `macro_file_reservation_cycle`" in generated
    assert "Renew with `renew_file_reservations`" in generated
    assert "`release_file_reservations`" in generated
    assert str(project) in generated
    assert str(tmp_path / "installed-agentstack") in generated
    assert "__AGENTSTACK_" not in generated


def test_codex_setup_renders_the_routes_once_in_an_isolated_home(
    tmp_path: pathlib.Path,
):
    home = tmp_path / "home"
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    target = codex_home / "AGENTS.md"
    target.write_text("# Personal instructions\n", encoding="utf-8")
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "AGENTSTACK_HOME": str(tmp_path / "installed-agentstack"),
        "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
        "AGENTSTACK_TEMPLATE_HOME": str(ROOT),
        "AGENTSTACK_PROJECT_KEY": "/fixture/canonical-project",
    }

    for _ in range(2):
        result = subprocess.run(
            ["/bin/bash", str(ROOT / "bin" / "agentstack-codex-setup")],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "installed managed block into" in result.stdout

    generated = target.read_text(encoding="utf-8")
    assert generated.count("<!-- >>> claude-agent-stack") == 1
    assert generated.count("<!-- <<< claude-agent-stack -->") == 1
    assert generated.count("# Personal instructions") == 1
    assert "### 1. Embedded canonical task" in generated
    assert "### 2. Bound ORRERY proxy" in generated
    assert "Bound proxy: follow its actual schema and use `reserve_files`" in generated
    assert "Renew with `renew_reservations`" in generated
    assert "release with `release_reservations`" in generated
    assert "Raw/direct MCP: acquire with `macro_file_reservation_cycle`" in generated
    assert "Renew with `renew_file_reservations`" in generated
    assert "`release_file_reservations`" in generated
    assert "/fixture/canonical-project" in generated
    assert str(tmp_path / "installed-agentstack") in generated
    assert "__AGENTSTACK_" not in generated
