"""Sanitized composer tails from the Mac A4 captures; no live terminal."""
from pathlib import Path
import runpy

import pytest

API = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'dashboard/terminal_state.py'))

# 2026-11-10 Codex 0.163.0 capture: only the private cwd is substituted.
CODEX_TAIL = '''  Worked for 37s • 13:58
                                             ⚠ weekly limit: 25% left · /status

› Ask Codex to do anything

  GPT-6.1-Sol default · /fixture/work
  ? for shortcuts                                     ⚠ 3 warnings · f2 to view
'''


@pytest.mark.parametrize('styled', [False, True])
def test_captured_codex_empty_composer_is_ready(styled):
    text = CODEX_TAIL
    if styled:
        text = text.replace('GPT-6.1-Sol', '\x1b[2mGPT-6.1-Sol').replace('/fixture/work', '/fixture/work\x1b[0m')
    assert API['compact_readiness'](text, 'codex') == dict(
        state='idle', input_empty=True, context_used_percent=None)


@pytest.mark.parametrize('model', ['GPT-6.1-Sol', 'gpt-model'])
@pytest.mark.parametrize('effort', ['default', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'])
def test_known_model_effort_footer_variants(model, effort):
    text = CODEX_TAIL.replace('GPT-6.1-Sol default', model + ' ' + effort)
    assert API['compact_refusal'](API['compact_readiness'](text, 'codex')) is None


@pytest.mark.parametrize('footer', ['⚠ 1 warning · f2 to view', '⚠ 3 warnings · f2 to view'])
def test_warning_footer_on_its_own_row(footer):
    text = CODEX_TAIL.rsplit('  ? for shortcuts', 1)[0] + footer
    assert API['compact_refusal'](API['compact_readiness'](text, 'codex')) is None


@pytest.mark.parametrize('extra', [
    'draft continuation', 'GPT-6.1-Sol invented · /fixture/work',
    'GPT-6.1-Sol default unexpected text', '⚠ unknown warning',
    '⚠ 3 warnings · f2 to view and submit',
])
def test_unknown_rows_below_empty_composer_stay_unknown(extra):
    value = API['compact_readiness'](CODEX_TAIL + extra, 'codex')
    assert value['state'] == 'unknown' and value['input_empty'] is None
    assert API['compact_refusal'](value) == 'TMUX_TARGET_NOT_IDLE'


@pytest.mark.parametrize('draft', ['pending input', 'Ask Codex to do anything else'])
def test_native_footer_does_not_hide_a_draft(draft):
    text = CODEX_TAIL.replace('Ask Codex to do anything', draft)
    value = API['compact_readiness'](text, 'codex')
    assert value['input_empty'] is False
    assert API['compact_refusal'](value) == 'TMUX_TARGET_INPUT_NOT_EMPTY'


def test_native_footer_does_not_hide_work_or_approval():
    for prefix in ('• Working (1s • esc to interrupt)',
                   'Press enter to confirm or esc to cancel'):
        value = API['compact_readiness'](prefix + '\n' + CODEX_TAIL, 'codex')
        assert API['compact_refusal'](value) == 'TMUX_TARGET_NOT_IDLE'


def test_captured_claude_empty_composer_is_ready():
    # 2026-11-10 morning capture: tail is literal, transcript omitted.
    text = ('✻ Cooked for 38s · done 8:50 AM\n\n' + '─' * 80 + '\n❯\u00a0\n'
            + '─' * 80 + '\n  ⏵⏵ auto mode on (shift+tab to cycle)\n\n\n\n')
    value = API['compact_readiness'](text, 'claude')
    assert value == dict(state='idle', input_empty=True, context_used_percent=None)
    assert API['compact_refusal'](value) is None


@pytest.mark.parametrize('rows', [
    ['GPT-6.1-Sol default · please review this transcript', '',
     'GPT-6.1-Sol default · /fixture/work', '? for shortcuts'],
    ['⚠ 3 warnings · f2 to view', 'GPT-6.1-Sol default · /fixture/work', '? for shortcuts'],
    ['? for shortcuts', 'GPT-6.1-Sol default · /fixture/work'],
    ['GPT-6.1-Sol default · /fixture/work', '? for shortcuts', '? for shortcuts'],
    ['GPT-6.1-Sol default · /fixture/work', '? for shortcuts', '⚠ 3 warnings · f2 to view'],
    ['gpt-model medium · draft', 'gpt-model medium · Context 70% left'],
    ['Context 70% left', 'GPT-6.1-Sol default · /fixture/work'],
    ['GPT-6.1-Sol default · /fixture/work', '? for shortcuts draft continuation'],
])
def test_footer_rows_have_bounded_count_and_order(rows):
    # Includes RockyTuring's empty-first-line multiline draft reproduction.
    value = API['compact_readiness']('›\n' + '\n'.join(rows), 'codex')
    assert value['state'] == 'unknown' and value['input_empty'] is None
    assert API['compact_refusal'](value) == 'TMUX_TARGET_NOT_IDLE'
