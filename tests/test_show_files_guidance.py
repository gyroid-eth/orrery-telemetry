"""Right after an update, an agent learns show_files from what it already reads."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(path):
    return (ROOT / path).read_text(encoding='utf-8')


def test_every_managed_block_has_the_section():
    for path in ('claude/CLAUDE.md', 'codex/AGENTS.md'):
        text = read(path)
        assert text.count('## Showing Files') == 1, path
        section = text.split('## Showing Files', 1)[1].split('\n## ', 1)[0]
        assert '`show_files`' in section and 'before\nyour final message' in section, path
        assert 'sends nothing' in section, path


def test_dsh_and_hermes_instructions_include_it():
    spec = importlib.util.spec_from_file_location('instruction_blocks', ROOT / 'bin/lib/instruction_blocks.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rendered = module.harness_template((ROOT / 'codex/AGENTS.md').read_bytes()).decode()
    assert '## Showing Files' in rendered and '`show_files`' in rendered


def test_bundled_skills_use_it():
    log = read('skills/log/SKILL.md').split('## Finish: Show The Log', 1)[1]
    assert "call ORRERY Mail's `show_files` with the log's path" in log
    assert 'whichever way the log was written' in log
    assert 'Skip it when `show_files` is not among your tools' in log
    delegate = read('skills/delegate/SKILL.md')
    assert 'Before reporting, call `show_files`' in delegate
    assert '7. Report the verified outcome to the user. Call `show_files`' in delegate


def test_claude_runs_it_without_a_prompt():
    allow = json.loads(read('hooks/settings.template.json'))['permissions']['allow']
    assert 'mcp__orrery-mail__show_files' in allow
