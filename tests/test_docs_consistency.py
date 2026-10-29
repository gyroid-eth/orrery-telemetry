"""Facts that user docs state about the implementation, checked against it.

Each check exists because a reader found the docs contradicting the code
(2026-09-03 first-look review): the ORRERY Mail port in AGENTS.md, the number
of approval prompts in install.md, the hook count and guide list in the
English README. The compact README now routes standalone setup facts to
getting-started and the complete document lists to the indexes.
"""
from __future__ import annotations

import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_pending_changelog_items_use_fragments() -> None:
    changelog = _read("CHANGELOG.md")
    import runpy

    validator = runpy.run_path(str(ROOT / "scripts/changelog-release.py"))["validate_changelog"]
    try:
        validator(changelog)
    except ValueError as error:
        raise AssertionError(str(error)) from error
    assert "[changelog.d/](changelog.d/)" in changelog
    assert (ROOT / "changelog.d/README.txt").is_file()


def test_pending_changelog_guard_rejects_direct_unreleased(monkeypatch) -> None:
    import pytest

    monkeypatch.setitem(globals(), "_read", lambda _: "## Unreleased\n\n### New\n")
    with pytest.raises(AssertionError, match="use changelog.d"):
        test_pending_changelog_items_use_fragments()


def test_changelog_fragments_are_valid() -> None:
    import runpy

    read_fragments = runpy.run_path(str(ROOT / "scripts/changelog-release.py"))["read_fragments"]
    read_fragments(ROOT)


def test_reservation_docs_cover_manual_release_after_bash_writes() -> None:
    for name in ("docs/hooks.md", "docs/hooks.en.md", "docs/launchers.md", "docs/launchers.en.md"):
        text = _read(name)
        for tool in ("`Bash`", "`release_reservations`", "`release_file_reservations`"):
            assert tool in text, (name, tool)


def _default_mail_port() -> str:
    # explicit > previous env.sh > this default (hooks/project-context.sh).
    match = re.search(
        r'^resolve_setting MCP_URL AGENTSTACK_MCP_URL http://127\.0\.0\.1:(\d+)/mcp kept$',
        INSTALLER,
        re.M,
    )
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
    assert "confirm_managed_setup()" not in INSTALLER
    assert "The engine owns the single consent prompt" in INSTALLER
    return confirms + managed_runs


def test_docs_share_the_actual_conditional_consent_contract() -> None:
    assert _approval_prompt_count() == 4  # Maximum approval sites, not a fixed prompt count.
    for name in ('AGENTS.md', 'CLAUDE.md', 'docs/install.en.md', 'docs/getting-started.en.md'):
        assert 'instruction-migration.en.md#consent' in _read(name)
        assert 'four typed confirmations' not in _read(name)
        assert 'four approvals' not in _read(name)
    for name in ('docs/install.md','docs/getting-started.md'):
        assert 'instruction-migration.md#consent' in _read(name)
    contract = _read('docs/instruction-migration.md')
    assert '最大4つ' in contract and '`[Y/n]`、Enter は消す' in contract
    assert '新しい利用者 file を作るだけなら同意は不要' in contract


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
    assert f"Claude event hook {count}件" in _read("docs/getting-started.md")
    words = {8: "Eight", 9: "Nine"}
    assert f"{words[count]} Claude event hooks" in _read("docs/getting-started.en.md")


def test_english_readme_lists_every_guide_the_japanese_readme_lists() -> None:
    ja_pattern = re.compile(r"^\|[^\n]*\[[^\]]+\]\((docs/[\w-]+\.md)\) \|", re.M)
    en_pattern = re.compile(r"^\|[^\n]*\[[^\]]+\]\((docs/[\w-]+(?:\.en)?\.md)\) \|", re.M)
    ja = ja_pattern.findall(_read("README.md"))
    en = [path.replace(".en.md", ".md") for path in en_pattern.findall(_read("README.en.md"))]
    assert ja, "no guide table in README.md"
    assert set(ja) == set(en), sorted(set(ja) ^ set(en))


def test_both_readmes_route_to_guides_and_standalone_setup() -> None:
    for name, setup, index in (("README.md", "docs/getting-started.md", "docs/README.md"),
                               ("README.en.md", "docs/getting-started.en.md", "docs/README.en.md")):
        text = _read(name)
        for needle in ("https://raw.githubusercontent.com/gyroid-eth/orrery/master/scripts/get.sh",
                       "Your first flight", "Show help map", "Full tour", setup, index):
            assert needle in text, (name, needle)
        for needle in ("--dry-run", "agentstack-doctor", "agentstack-selftest", "/delegate", "http://127.0.0.1:8770/"):
            assert needle in _read(setup), (setup, needle)
    for image in ("img/deck.jpg", "img/network.jpg", "img/new-agent.jpg"):
        assert image in _read("docs/getting-started.md"), image
        assert image in _read("docs/getting-started.en.md"), image


def test_documentation_indexes_cover_the_actual_tree() -> None:
    import runpy

    checker = runpy.run_path(str(ROOT / "scripts/check_docs_index.py"))
    assert checker["check"](ROOT) == []


def test_api_docs_state_the_served_api_generation_and_when_it_changes() -> None:
    # 2026-09-30: ORRERY cockpit decides whether orrery-telemetry is new
    # enough from `api`, not from the release date, so the docs must show the
    # generation the server really serves and the rule for raising it.
    import ast
    source = _read("dashboard/server.py")
    definitions = [node.value.value for node in ast.parse(source).body
                   if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "API_GENERATION" for target in node.targets)
                   and isinstance(node.value, ast.Constant) and type(node.value.value) is int]
    assert len(definitions) == 1, "one API_GENERATION source is required"
    assert '"api": API_GENERATION' in source, "/api/version must use the shared generation"
    generation = definitions[0]
    for rel, rule in (("docs/api.md", "互換の世代"), ("docs/api.en.md", "compatibility generation")):
        doc = _read(rel)
        assert f'"api":{generation}}}' in doc, f"{rel} shows another api generation"
        assert rule in doc and "CHANGELOG" in doc, f"{rel} does not say when api goes up"


def test_protection_setting_is_documented_across_launch_and_agent_guides() -> None:
    example = _read(".env.example")
    assert "AGENTSTACK_EXTRA_PROTECTED_ROOTS=/path/to/shared-vault" in example
    assert "AGENTSTACK_PROTECTED_ROOTS=" not in example
    for rel in (
        "AGENTS.md", "README.md", "README.en.md",
        "docs/configuration.md", "docs/configuration.en.md",
        "docs/install.md", "docs/install.en.md", "docs/hooks.md", "docs/hooks.en.md",
        "docs/launchers.md", "docs/launchers.en.md",
        "docs/delegation.md", "docs/delegation.en.md",
        "claude/CLAUDE.md", "codex/AGENTS.md",
    ):
        assert "AGENTSTACK_EXTRA_PROTECTED_ROOTS" in _read(rel), rel
    for rel in ("claude/CLAUDE.md", "codex/AGENTS.md"):
        assert "physical absolute file paths" in _read(rel), rel
        assert "Do not carry a previous" in _read(rel), rel


def test_automatic_protection_migration_and_cutover_are_documented() -> None:
    # The empty marker and installed-file source are safety boundaries, not
    # optional cleanup advice; both install guides and references must say so.
    for rel in ("docs/configuration.md", "docs/configuration.en.md",
                "docs/install.md", "docs/install.en.md"):
        doc = _read(rel)
        for needle in ("AGENTSTACK_EXTRA_PROTECTED_ROOTS=", "AGENTSTACK_PROTECTED_ROOTS",
                       "--dry-run", "--reset-settings", "shell/tmux", "marker",
                       "direct/unmanaged", "cutover"):
            assert needle in doc, (rel, needle)
    english = _read("docs/configuration.en.md")
    for needle in ("using presence rather than non-emptiness", "copied exactly once",
                   "literal value and order intact", "no manual copy is needed",
                   "source, value, and destination", "before the installed file is overwritten",
                   "even when extras already exist", "physical absolute path",
                   "restart affected sessions together", "Namespace selection is unchanged"):
        assert needle in english, needle
    japanese = _read("docs/configuration.md")
    for needle in ("設定の有無", "そのまま一度だけ", "手動コピーは不要", "移行元・値・保存先",
                   "上書きする前", "extras が既に存在しても警告", "物理絶対 path",
                   "session をまとめて再起動", "namespace の選択は変えず"):
        assert needle in japanese, needle
    assert '"AGENTSTACK_EXTRA_PROTECTED_ROOTS": os.environ["AGENTSTACK_INSTALL_EXTRA_PROTECTED_ROOTS"]' in INSTALLER
    assert "would migrate installed AGENTSTACK_PROTECTED_ROOTS to AGENTSTACK_EXTRA_PROTECTED_ROOTS once" in INSTALLER
    assert "source=%s value=%q destination=%s:AGENTSTACK_EXTRA_PROTECTED_ROOTS" in INSTALLER
    assert "ignoring ambient roots (never migrated)" in INSTALLER


def test_workspace_protection_does_not_change_warm_pool_selection() -> None:
    child = _read("hooks/spawn_child.sh")
    assert "claim-workspace-v1" not in child
    assert 'WARM_STATUS=$(bash "$WARM_POOL" status' in child
    assert 'claim_warm_session "$WARM_TYPE" "$CHILD_NAME" "$CHILD_MODEL"' in child
    assert '"${AGENTSTACK_PROTECTION_CONTEXT:-}" == workspace-v1' not in child
    for rel in ("docs/launchers.md", "docs/launchers.en.md",
                "docs/delegation.md", "docs/delegation.en.md"):
        assert "warm pool" in _read(rel).replace("warm-pool", "warm pool"), rel


def test_child_tool_docs_explain_approval_refusal_reports_and_repair():
    report = "`MCP tool <tool-name> is blocked: <exact message>.`"
    for path in ("docs/delegation.md", "docs/delegation.en.md"):
        section = _read(path).split("### " + (
            "MCP tool が承認の方針で拒否されたとき" if path.endswith("delegation.md")
            else "When an MCP tool is refused by the approval policy"
        ), 1)[1].split("\n### ", 1)[0]
        assert section.count(report) == 1, path
        for literal in ("`approval policy is never`", "`--allowed-tools`",
                        "`~/.codex/config.toml`", "`~/.claude/settings.json`",
                        "`send_message`", "standalone", "Allow"):
            assert literal in section, (path, literal)
        assert "agentstack-resume" not in section
        assert "--codex-child-overlay" not in section
    english = _read("docs/delegation.en.md")
    assert "When an MCP tool returns a refusal message" in english
    assert "A pending Allow prompt blocks the tool call: Allow lets it continue; if permission is denied and a refusal message returns, the child reports it then." in english
    for link in (
        "(#approval)", "(#choosing-the-tools-a-child-gets---base----tools)",
        "(#resume-a-stopped-child-with-different-tools)",
        "(troubleshooting.en.md#mcp-tool-call-requires-approval-but-approval-policy-is-never)",
        "(#when-another-mcp-is-needed-partway-through-a-task)",
        "(#what-a-person-does-setup-and-permission-for-connections-and-operations)",
    ):
        assert link in english
    japanese = _read("docs/delegation.md")
    assert "MCP tool から拒否メッセージが返った場合" in japanese
    assert "Allow 待ちの間は tool の呼出し自体が止まります。人が Allow を押せば続き、許可が拒否されてメッセージが返れば、そのとき子が報告します。" in japanese
    for link in (
        "(#承認)", "(#子に渡す道具を選ぶ--base----tools)",
        "(#停止した子の道具を変更して-resume-する)",
        "(troubleshooting.md#mcp-tool-call-requires-approval-but-approval-policy-is-never)",
        "(#途中で必要な-mcp-が増えたとき)",
        "(#人がやること事前の設定と接続操作時の許可)",
    ):
        assert link in japanese
