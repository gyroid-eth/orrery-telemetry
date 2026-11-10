"""A4 screen and process evidence regressions; no live panes or services."""
import os
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = runpy.run_path(str(ROOT / 'dashboard/terminal_state.py'))


@pytest.mark.parametrize('text', [
    '› Ask Codex to do anything\ngpt-model medium · Context 70% left',
    '›\u00a0Ask Codex to do anything\n? for shortcuts',
])
def test_observed_codex_placeholder_is_an_empty_composer(text):
    value = TERMINAL['compact_readiness'](text, 'codex')
    assert value['state'] == 'idle' and value['input_empty'] is True
    assert TERMINAL['compact_refusal'](value) is None


@pytest.mark.parametrize('suffix', [
    'Ask Codex to do anything else', 'Ask a different assistant',
    '親からの RELEASE_REQUEST を確認して',
    '親から COMPLETE が届いたので確認して進めてください', '/quit',
])
@pytest.mark.parametrize('provider,glyph', [('codex', '›'), ('claude', '❯')])
def test_unknown_placeholder_and_observed_claude_suggestions_remain_pending_input(provider, glyph, suffix):
    value = TERMINAL['compact_readiness'](glyph + ' ' + suffix, provider)
    assert value['input_empty'] is False
    assert TERMINAL['compact_refusal'](value) == 'TMUX_TARGET_INPUT_NOT_EMPTY'


def test_observed_claude_empty_composer_with_status_footer():
    value = TERMINAL['compact_readiness']('❯\u00a0\n────────────────\n⏵⏵ auto mode on (shift+tab to cycle)', 'claude')
    assert value['state'] == 'idle' and value['input_empty'] is True


@pytest.mark.parametrize('locale', ['C', 'ja_JP.UTF-8'])
def test_birth_record_and_snapshot_share_c_locale(monkeypatch, locale):
    api = runpy.run_path(str(ROOT / 'bin/lib/process_observation.py'))
    monkeypatch.setenv('LC_ALL', locale)
    monkeypatch.setenv('LANG', locale)
    monkeypatch.setenv('A4_ENV_SENTINEL', 'preserved')
    seen = []
    def ps(argv, **kwargs):
        seen.append(kwargs.get('env', {}))
        # Same process observed on macOS with C and ja_JP.UTF-8, including
        # differing trailing padding. The original probe is kept privately.
        birth = 'Wed Oct 21 22:30:25 2026    ' if kwargs.get('env', {}).get('LC_ALL') == 'C' else '水 10/21 22:30:25 2026     '
        return SimpleNamespace(returncode=0, stderr='', stdout=birth if '-p' in argv else '    1     0     0 ' + birth)
    monkeypatch.setattr(api['subprocess'], 'run', ps)
    assert api['process_start'](1) == 'Wed Oct 21 22:30:25 2026'
    assert api['process_snapshot']()[1] == (0, 0, 'Wed Oct 21 22:30:25 2026')
    assert len(seen) == 2
    assert all(env['LC_ALL'] == 'C' and env['A4_ENV_SENTINEL'] == 'preserved' for env in seen)
    assert os.environ['LC_ALL'] == locale
