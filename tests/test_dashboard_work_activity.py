"""Current work and recorded event age remain separate through the real card."""
from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

import pytest

from dashboard import server, terminal_state
from test_dashboard_runtime_cache import _CLAUDE_USAGE_INPUT, _OLD_WORK


class Card(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.classes = []
        self.words = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.classes.extend(dict(attrs).get('class', '').split())

    def handle_data(self, data):
        self.words.append(data)

    @property
    def text(self):
        return ' '.join(self.words)


def render_card(row):
    """Execute the shipped renderer; stub only unrelated display helpers."""
    source = (Path(__file__).parents[1] / 'dashboard/index.html').read_text()
    renderer = re.search(r'function bay\(a,i\)\{.*?\n}', source, re.S).group()
    helpers = """
const esc=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;')
  .replaceAll('>','&gt;').replaceAll('"','&quot;');
const isResumeCategory=()=>false, scientistOf=()=>null, avatarKeyOf=()=>null;
const canKill=()=>false, deckProviderLogo=()=>'';
"""
    result = subprocess.run(
        ['node', '-e', helpers + renderer + '\nprocess.stdout.write(bay('
         + json.dumps(row) + ',0));'], capture_output=True, text=True,
        timeout=10, check=True,
    )
    return Card(result.stdout)


@pytest.mark.parametrize('current,state,duration', [
    ('⏺ Running a synthetic task · 1m 1s\n'
     '  ⎿  $ synthetic command\n'
     '✻ Herding… (54m 18s · ↓ 134.2k tokens)\n', 'work', '54m18s'),
    ('✽ Inspecting files…\n', 'work', 'WORKING'),
    (_OLD_WORK + '\n⏺ Finished the requested work.\n', 'wait', None),
])
def test_current_work_with_old_events_survives_runtime_roster_and_card(
    monkeypatch, current, state, duration,
):
    capture = current + _CLAUDE_USAGE_INPUT
    monkeypatch.setattr(server.time, 'time', lambda: 10_000)
    monkeypatch.setattr(server.time, 'monotonic', lambda: 100.0)
    monkeypatch.setattr(server, '_rt_cache', {})
    monkeypatch.setattr(server, '_provider_captures', {})
    monkeypatch.setattr(server, '_observed', {})
    monkeypatch.setattr(server, '_tmux_run', lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=capture))
    monkeypatch.setattr(server, 'tmux_state', lambda: {'FixtureAgent': dict(
        cmd='claude', title='', activity=8_000, created=100,
        session_id='$fixture', attached=False, pane_pid=61000)})
    monkeypatch.setattr(server, 'agentmail_state', lambda: ({'FixtureAgent': dict(
        program='claude-code', model='Opus 5.5', model_raw='Opus 5.5',
        task='', last_active=8_500)}, {}))
    monkeypatch.setattr(server, '_process_tree_snapshot', lambda:
                        ({61000: 'zsh', 61010: 'claude'}, {61000: [61010]}))
    for name in ('_codex_app_runtimes', '_deliverables_index',
                 '_name_substitutions', '_persistent_profiles'):
        monkeypatch.setattr(server, name, lambda: {})
    monkeypatch.setattr(server, '_project_key', lambda: '')
    monkeypatch.setattr(server, '_retired_names', lambda *_: set())
    monkeypatch.setattr(server, '_resume_capability_for_row', lambda *a, **kw: 'ready')
    monkeypatch.setattr(server, '_claude_row_mail_fields', lambda *a, **kw: {})
    monkeypatch.setattr(server.harness_runtime, 'metadata', lambda *a: {})

    row, = server.build_agents()
    assert row['running'] is True and row['act_state'] == state
    assert row['last_active'] == 8_500 and row['mail_active'] == 8_500
    assert row['last_active_rel'] == '25m 前'
    assert row['ctx_used'] is None and row['live'] == ''
    if state == 'work':
        assert terminal_state.activity(capture) == 'work'
    card = render_card(row)
    assert 'LAST EVENT 25m 前' in card.text
    assert 'NO LIVE SUMMARY' in card.text
    assert 'STANDBY' not in card.text
    assert ('work' in card.classes) is (state == 'work')
    if duration:
        assert duration in card.text


@pytest.mark.parametrize('running,state,indicator', [
    (True, 'work', 'WORKING'), (True, 'ask', 'APPROVAL'),
    (True, 'question', '?'), (True, 'wait', None),
    (True, None, None), (False, 'work', None),
])
def test_missing_summary_does_not_invent_activity_or_hide_reported_work(
    running, state, indicator,
):
    row = dict(name='FixtureAgent', category='agent', running=running,
               act_state=state, live='', task='', last_active_rel='—')
    card = render_card(row)
    assert 'LAST EVENT —' in card.text
    assert 'NO LIVE SUMMARY' in card.text and 'STANDBY' not in card.text
    assert ('work' in card.classes) is (running and state == 'work')
    if indicator and running:
        assert indicator in card.text
    else:
        assert 'WORKING' not in card.text and 'APPROVAL' not in card.text


def test_codex_app_registering_uses_existing_reported_work_without_duration(monkeypatch):
    monkeypatch.setattr(server.time, 'time', lambda: 10_000)
    observed = server._codex_app_live(dict(
        state='registering', last_seen_at='1970-01-01T02:46:30Z'))
    assert observed['running'] is True and observed['act_state'] == 'work'
    card = render_card(dict(name='FixtureApp', category='agent', task='',
                            last_active_rel='25m 前', **observed))
    assert 'WORKING' in card.text and 'work' in card.classes
    assert 'Codex App · registering' in card.text
    assert 'LAST EVENT 25m 前' in card.text
