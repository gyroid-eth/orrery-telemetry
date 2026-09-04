"""Facts that user docs state about the implementation, checked against it.

Each check exists because a reader found the docs contradicting the code
(2026-09-03 first-look review): the agent-mail port in AGENTS.md, the number
of approval prompts in install.md, the hook count and guide list in the
English README.
"""
from __future__ import annotations

import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _default_mail_port() -> str:
    match = re.search(r'^MCP_URL="\$\{AGENTSTACK_MCP_URL:-http://127\.0\.0\.1:(\d+)/mcp\}"$', INSTALLER, re.M)
    assert match, "installer default MCP_URL not found"
    return match.group(1)


def test_agents_md_probes_the_installer_default_mail_port() -> None:
    port = _default_mail_port()
    agents = _read("AGENTS.md")
    assert f"lsof -i :{port}" in agents
    assert f"already listening on {port}" in agents
    stale = {"8765", "18765"} - {port}
    for other in stale:
        assert f":{other}" not in agents, f"AGENTS.md still names port {other}"


def _approval_prompt_count() -> int:
    # One `read -r reply` per confirm_* function, and the managed-setup
    # confirmation runs once per run_managed_setup call.
    confirms = len(re.findall(r"^confirm_\w+\(\) \{$", INSTALLER, re.M))
    managed_runs = len(re.findall(r'^\s*run_managed_setup "', INSTALLER, re.M))
    assert "confirm_managed_setup()" in INSTALLER
    return confirms - 1 + managed_runs


def test_docs_state_the_real_number_of_approval_prompts() -> None:
    count = _approval_prompt_count()
    assert count == 4, count
    assert f"{count} つの承認" in _read("docs/install.md")
    assert f"{count} 回 `yes`" in _read("README.md")
    words = {4: "four"}
    assert f"`yes` {words[count]} times" in _read("README.en.md")
    assert f"`yes` {words[count]} times" in _read("AGENTS.md")
    assert "once more" not in _read("AGENTS.md")


def _event_hook_count() -> int:
    template = json.loads(_read("hooks/settings.template.json"))
    commands = set()
    for matchers in template["hooks"].values():
        for matcher in matchers:
            for hook in matcher["hooks"]:
                commands.add(hook["command"])
    return len(commands)


def test_hook_count_matches_the_settings_template() -> None:
    count = _event_hook_count()
    assert f"event hook は{count}件" in _read("docs/hooks.md")
    assert f"Claude event hook {count}件" in _read("README.md")
    words = {8: "Eight"}
    assert f"{words[count]} Claude event hooks" in _read("README.en.md")


def test_english_readme_lists_every_guide_the_japanese_readme_lists() -> None:
    ja_pattern = re.compile(r"^\| \[[^\]]+\]\((docs/[\w-]+\.md)\) \|", re.M)
    en_pattern = re.compile(r"^\| \[[^\]]+\]\((docs/[\w-]+(?:\.en)?\.md)\) \|", re.M)
    ja = ja_pattern.findall(_read("README.md"))
    en = [path.replace(".en.md", ".md") for path in en_pattern.findall(_read("README.en.md"))]
    assert ja, "no guide table in README.md"
    assert set(ja) == set(en), sorted(set(ja) ^ set(en))


def test_both_readmes_share_the_quick_start_commands() -> None:
    for name in ("README.md", "README.en.md"):
        text = _read(name)
        for needle in ("--dry-run", "agentstack-doctor", "agentstack-selftest", "/delegate", "http://127.0.0.1:8770/"):
            assert needle in text, (name, needle)
        for image in ("docs/img/deck.jpg", "docs/img/network.jpg", "docs/img/new-agent.jpg"):
            assert image in text, (name, image)
