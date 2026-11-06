"""show_files names files for the cockpit's History; it never sends, writes or reads them."""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
from pathlib import Path

import pytest

from agentstack_mail import fresh_proxy, show_files

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def work(tmp_path):
    (tmp_path / 'work/docs').mkdir(parents=True)
    (tmp_path / 'work/docs/report.md').write_text('# private body\n')
    (tmp_path / 'work/figure.webp').write_bytes(b'RIFF')
    (tmp_path / 'work/link.png').symlink_to(tmp_path / 'work/figure.webp')
    (tmp_path / 'elsewhere.md').write_text('outside\n')
    return tmp_path / 'work'


def test_with_a_known_working_folder_paths_are_checked_with_lstat_only(work):
    value = show_files.result(['docs/report.md', str(work / 'figure.webp'), 'link.png', 'docs', 'gone.md'],
                              base=str(work))
    assert value['present'] == ['docs/report.md', str(work / 'figure.webp')]
    kinds = {item['path']: item.get('kind') for item in value['files']}
    assert kinds == {'docs/report.md': 'file', str(work / 'figure.webp'): 'image', 'link.png': 'other',
                     'docs': 'other', 'gone.md': None}
    assert 'private body' not in json.dumps(value)


def test_outside_the_working_folder_is_not_even_looked_at(work):
    outside = [str(work.parent / 'elsewhere.md'), '../elsewhere.md', 'docs/../../elsewhere.md']
    value = show_files.result(outside, base=str(work))
    assert value['files'] == [{'path': path, 'outside_working_folder': True} for path in outside]
    assert value['present'] == []


def test_without_a_known_folder_relative_paths_are_left_to_the_cockpit(work, tmp_path, monkeypatch):
    # The proxy's own cwd (a plugin cache, say) is never the base: no "missing" verdict.
    monkeypatch.chdir(tmp_path)
    value = show_files.result(['docs/report.md', 'figure.webp', str(work / 'figure.webp')])
    assert value['files'][:2] == [{'path': 'docs/report.md', 'checked': False}, {'path': 'figure.webp', 'checked': False}]
    assert value['files'][2]['kind'] == 'image'                # an absolute path is still checked
    assert not any(item.get('exists') is False for item in value['files'])
    assert 'checked:false' in value['note']


@pytest.mark.parametrize('paths', [[], ['a'] * 9, ['~/a'], ['file:///a'], ['a\x00b'], [1], 'a', None])
def test_only_one_to_eight_plain_paths(paths):
    with pytest.raises(ValueError):
        show_files.result(paths)


def test_both_proxies_publish_the_same_words_and_results(work):
    source = (ROOT / 'integrations/codex_app/src/agentstack_codex_app/mcp_server.py').read_text()
    block = re.search(r'SHOW_FILES_DESCRIPTION = \((.*?)\n\)', source, re.S)[1]
    assert ''.join(re.findall(r'"((?:[^"\\]|\\.)*)"', block)) == show_files.DESCRIPTION
    sys.path.insert(0, str(ROOT / 'integrations/codex_app/src'))
    from agentstack_codex_app.mcp_server import ProxyError, show_files_result
    cases = [(['docs/report.md', 'figure.webp', 'link.png', 'docs', 'gone.md', '../elsewhere.md'], str(work)),
             (['docs/report.md', str(work / 'figure.webp')], None),
             ([str(work.parent / 'elsewhere.md')], str(work))]
    for paths, base in cases:
        assert show_files_result(paths, base) == show_files.result(paths, base), (paths, base)
    for paths in ([], ['~/a'], ['a\x00b'], [1]):
        with pytest.raises(ValueError):
            show_files.result(paths)
        with pytest.raises(ProxyError):
            show_files_result(paths)


class CountingClient:
    """Stands in for the bound upstream; show_files must never reach it."""
    path = Path('/fixture/client')

    def __init__(self):
        self.calls = []

    def _load(self):
        return {}, None

    async def call(self, name, args):
        self.calls.append(name)
        return {}


def run_fresh(monkeypatch, requests, resolve_external=None):
    async def catalogue(client):
        return {'show_files': dict(name='show_files', description=show_files.DESCRIPTION,
                                   inputSchema=show_files.INPUT_SCHEMA)}
    monkeypatch.setattr(fresh_proxy, 'catalogue', catalogue)
    client = CountingClient()
    lines = [{'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {}}] + requests
    output = io.StringIO()
    asyncio.run(fresh_proxy.serve(client, io.StringIO(''.join(json.dumps(x) + '\n' for x in lines)), output,
                                  resolve_external=resolve_external))
    return client, [json.loads(line) for line in output.getvalue().splitlines()][1:]


def call(paths, request_id=1):
    return {'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call',
            'params': {'name': 'show_files', 'arguments': {'paths': paths}}}


def test_fresh_proxy_answers_show_files_without_any_upstream_call(monkeypatch, work):
    client, replies = run_fresh(monkeypatch, [call([str(work / 'docs/report.md'), 'figure.webp'])])
    value = replies[0]['result']['structuredContent']
    assert replies[0]['result']['isError'] is False
    assert value['present'] == [str(work / 'docs/report.md')]
    assert value['files'][1] == {'path': 'figure.webp', 'checked': False}
    assert 'private body' not in json.dumps(replies)
    assert client.calls == []


def test_fresh_proxy_needs_the_binding_first(monkeypatch, work):
    client, replies = run_fresh(monkeypatch, [call(['figure.webp'])], resolve_external=lambda *_: None)
    assert replies[0]['result']['isError'] is True
    assert replies[0]['result']['structuredContent']['code'] == 'BOOTSTRAP_REQUIRED'
    assert client.calls == []
