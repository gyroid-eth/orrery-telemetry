"""show_files names files for the cockpit's History; it never sends, writes or reads them."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agentstack_mail import show_files

ROOT = Path(__file__).resolve().parents[3]


def test_paths_are_checked_with_lstat_only(tmp_path):
    (tmp_path / 'docs').mkdir()
    (tmp_path / 'docs/report.md').write_text('# private body\n')
    (tmp_path / 'figure.webp').write_bytes(b'RIFF')
    (tmp_path / 'link.png').symlink_to(tmp_path / 'figure.webp')
    value = show_files.result(['docs/report.md', 'figure.webp', 'link.png', 'docs', 'gone.md'], cwd=str(tmp_path))
    assert value['shown'] == ['docs/report.md', 'figure.webp']
    kinds = {item['path']: item.get('kind') for item in value['files']}
    assert kinds == {'docs/report.md': 'file', 'figure.webp': 'image', 'link.png': 'other', 'docs': 'other', 'gone.md': None}
    assert 'private body' not in json.dumps(value)


@pytest.mark.parametrize('paths', [[], ['a'] * 9, ['~/a'], ['file:///a'], ['a\x00b'], [1], 'a', None])
def test_only_one_to_eight_plain_paths(paths):
    with pytest.raises(ValueError):
        show_files.result(paths)


def test_both_proxies_publish_the_same_words():
    source = (ROOT / 'integrations/codex_app/src/agentstack_codex_app/mcp_server.py').read_text()
    block = re.search(r'SHOW_FILES_DESCRIPTION = \((.*?)\n\)', source, re.S)[1]
    words = ''.join(re.findall(r'"((?:[^"\\]|\\.)*)"', block))
    assert words == show_files.DESCRIPTION
