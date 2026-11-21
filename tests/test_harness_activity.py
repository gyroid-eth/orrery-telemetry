"""Pinned native UI vocabulary, active-footer boundaries and one-capture wiring."""
from types import SimpleNamespace
from pathlib import Path
import json
import textwrap

import pytest

from dashboard.harness_activity import parse_activity
from dashboard import server


@pytest.mark.parametrize('program,text,state', [
    ('dsh', 'ctx 19% (24k/128k)\nesc to interrupt', 'work'),
    ('dsh', 'ctx 19% (24k/128k)\nesc 中断', 'work'),
    ('dsh', '── Awaiting approval · bash ──\nAllow this operation?\n❯ 1. Yes, allow once\n'
     '↑/↓ select · Enter confirm · Esc reject\nctx 19% (24k/128k)\nesc to interrupt', 'ask'),
    ('dsh', '── ⏳ 等待审批 · bash ──\n要允许这次操作吗？\n❯ 1. 允许（仅本次）\n'
     '↑/↓/Tab 选择 · Enter 确认 · 选中拒绝后可输入理由 · Esc 拒绝', 'ask'),
    ('dsh', '📋 Question 1/2 · 1 left\nChoose a result\n'
     '↑/↓ select · Space multi-select · Enter submit · Esc cancel · Ctrl+Q to fold / click the header', 'question'),
    ('dsh', '📋 提问 · 第 1/2 题\n问题\n'
     '输入回答 · Ctrl+V 粘贴 · Enter 提交 · Esc 上一题 · Ctrl+Q 折叠 / 点标题行收起', 'question'),
    ('dsh', '▸ 📋 Question 1/2 · 1 left Choose a result\n⏸ Waiting for your answer — Ctrl+Q to expand', 'question'),
    ('dsh', '▸ 📋 提问 · 第 1/2 题 问题\n正在等你回答 — Ctrl+Q 展开', 'question'),
    ('hermes', '☤ ❯ msg=interrupt · /queue · /bg · /steer · Ctrl+C cancel\n☤ model │ 24k/128K │ 19%', 'work'),
    ('hermes', '☤ メッセージ=中断 · /queue · /bg · /steer · Ctrl+C でキャンセル', 'work'),
    ('hermes', '╭── ⚠️  Dangerous Command ──╮\n│ echo fixture │\n│ ❯ 1. Allow once │\n╰────╯\n'
     '↑/↓ to select, Enter to confirm  (39s)', 'ask'),
    ('hermes', '⚠️  危険なコマンド\n❯ 1. 今回のみ許可\n↑/↓ で選択、Enter で確定', 'ask'),
    ('hermes', 'ExampleAgent needs your input\nPick one\n'
     '↑/↓ to select, Enter to lock, Tab next question  (29s)', 'question'),
    ('hermes', 'ExampleAgent needs your input\nWhy?\ntype your answer and press Enter', 'question'),
    ('hermes', 'ExampleAgent が入力を求めています\n理由は？\n回答を入力して Enter を押してください', 'question'),
    ('hermes', 'ExampleAgent が入力を求めています\n選択\n↑/↓ で選択、Enter で確定、Tab で次の質問', 'question'),
])
def test_pinned_current_controls(program, text, state):
    if program == 'hermes' and state == 'work':
        prompt = text.splitlines()[0]
        text = '─' * 80 + '\n' + prompt + '\n' + '─' * 80
    elif program == 'hermes':
        prompt = '⚠ ❯' if state == 'ask' else '? ❯'
        text += '\n' + '─' * 80 + '\n' + prompt + '\n' + '─' * 80
    assert parse_activity(text, program) == state
    styled = '\x1b[38;2;1;2;3m' + text.replace('\n', '\x1b[0m\n\x1b[1m') + '\x1b[0m'
    assert parse_activity(styled, program) == state
    # Controls in old transcript rows must not become current activity.
    assert parse_activity(text + '\nnew output' * 46, program) is None


@pytest.mark.parametrize('program,text', [
    ('dsh', 'Thinking…'), ('dsh', '? for shortcuts'),
    ('dsh', 'Awaiting approval · bash'),
    ('dsh', '↑/↓ select · Enter confirm · Esc reject'),
    ('dsh', '📋 Question 1/2\nEnter submit'),
    ('dsh', 'Awaiting approval · bash\nesc to interrupt'),
    ('dsh', '📋 Question 1/2\nesc to interrupt'),
    ('dsh', '> Awaiting approval · bash\n> ↑/↓ select · Enter confirm · Esc reject'),
    ('dsh', '↑/↓ select · Enter confirm · Esc reject\nAwaiting approval · bash'),
    ('hermes', 'Dangerous Command'),
    ('hermes', '⚠️  Dangerous Command\n↑/↓ to select, Enter to conf'),
    ('hermes', 'ExampleAgent needs your input\nmsg=interrupt · /queue · /bg · /steer · Ctrl+C cancel'),
    ('hermes', '↑/↓ to select, Enter to confirm'),
    ('hermes', 'Ask something to get started'),
    ('hermes', 'Working…'), ('hermes', 'Ctrl+C cancel'),
    ('hermes', 'ExampleAgent needs your input\nunsupported locale footer'),
    ('hermes', '⚠️  Dangerous Command\n↑/↓ to select, Enter to confirm\n' + 'new output\n' * 8),
    ('dsh', 'ctx 19% (24k/128k)'), ('hermes', '☤ model │ 24k/128K │ 19%'),
    ('claude', 'esc to interrupt'), ('codex', 'Working (2s)'),
    ('dsh', None), ('hermes', ''),
])
def test_missing_quoted_old_or_incomplete_evidence_stays_unknown(program, text):
    assert parse_activity(text, program) is None


@pytest.mark.parametrize('program,working', [
    ('dsh', 'ctx 19% (24k/128k)\nesc to interrupt'),
    ('hermes', '☤ ❯ msg=interrupt · /queue · /bg · /steer · Ctrl+C cancel\n☤ model │ 24k/128K │ 19%'),
])
def test_one_capture_cache_generation_and_unknown_transition(monkeypatch, program, working):
    if program == 'hermes':
        prompt, status = working.splitlines()
        working = '\n'.join([status, '─' * 80, prompt, '─' * 80])
    server._NATIVE_CONTEXT_CACHE.clear()
    clock, text, calls = [100.0], [working], []
    monkeypatch.setattr(server.time, 'monotonic', lambda: clock[0])

    def capture(argv, **kwargs):
        calls.append(argv)
        assert argv == ['capture-pane', '-p', '-J', '-t', '=ExampleAgent', '-S', '-45']
        return SimpleNamespace(returncode=0, stdout=text[0])

    monkeypatch.setattr(server, '_tmux_run', capture)
    value = server._native_context_runtime('ExampleAgent', 1, '$1', program)
    # dsh also reports the effort from its footer; Hermes does not.
    extra = {'dsh_card_effort': None} if program == 'dsh' else {}
    assert value == {'ctx_used': 19, 'ctx_window': '128K', 'act_state': 'work', **extra}
    assert server._native_context_runtime('ExampleAgent', 1, '$1', program) == value
    assert len(calls) == 1
    text[0] = 'status hidden'
    # Same session name, new generation: never reuse the old work observation.
    assert server._native_context_runtime('ExampleAgent', 2, '$2', program) == {
        'ctx_used': None, 'ctx_window': None, 'act_state': None, **extra}
    text[0] = working
    clock[0] += server._RT_TTL + 1
    assert server._native_context_runtime('ExampleAgent', 2, '$2', program)['act_state'] == 'work'
    clock[0] += server._RT_TTL + 1
    monkeypatch.setattr(server, '_tmux_run', lambda *a, **k: SimpleNamespace(returncode=1))
    assert server._native_context_runtime('ExampleAgent', 2, '$2', program) == {
        'ctx_used': None, 'ctx_window': None, 'act_state': None}


@pytest.mark.parametrize('text,state', [
    ('esc to interrupt', 'work'),
    ('Enter to select · Esc to cancel', 'question'),
    ('Press enter to confirm or esc to cancel', 'ask'),
    ('❯', 'wait'),
])
def test_existing_claude_codex_classification_is_unchanged(text, state):
    assert server._parse_runtime(text)['act_state'] == state


_CAPTURES = Path(__file__).parent / 'fixtures/native_activity'


@pytest.mark.parametrize('program,file,state', [
    ('hermes', 'hermes-idle.txt', 'wait'),
    ('hermes', 'hermes-work.txt', 'work'),
    ('hermes', 'hermes-consent.txt', None),
    ('dsh', 'dsh-idle.txt', 'wait'),
    ('dsh', 'dsh-work.txt', 'work'),
    ('dsh', 'dsh-resume-idle.txt', 'wait'),
])
def test_real_acceptance_capture_footers(program, file, state):
    # Exact unmodified excerpts; the surrounding transcript is deliberately
    # excluded to keep private paths and agent identities out of public tests.
    text = (_CAPTURES / file).read_text()
    assert parse_activity(text, program) == state
    assert parse_activity(text + '\nnew transcript' * 46, program) is None


@pytest.mark.parametrize('profile', ['fixture-profile', 'custom', 'model-123', ''])
def test_hermes_prompt_shape_survives_profile_and_translation_changes(profile):
    idle = (_CAPTURES / 'hermes-idle.txt').read_text().replace('deepseek ❯', (profile + ' ❯').strip())
    assert parse_activity(idle, 'hermes') == 'wait'
    busy = (_CAPTURES / 'hermes-work.txt').read_text()
    for placeholder in ('cli.tui.placeholder_busy', 'msg=interrupt', '翻訳した入力の案内', ''):
        assert parse_activity(busy.replace('cli.tui.placeholder_busy', placeholder), 'hermes') == 'work'


@pytest.mark.parametrize('program,file', [('hermes', 'hermes-idle.txt'), ('dsh', 'dsh-idle.txt')])
def test_wait_requires_a_complete_current_input_frame(program, file):
    text = (_CAPTURES / file).read_text()
    frame = next(row for row in text.splitlines() if row.strip().startswith(('╭─', '────────')))
    # The active control can disappear, be clipped, or leave a stale frame above
    # new output. None of these is proof of waiting for input.
    for broken in (text.replace('❯', ''), text.replace(frame, frame.replace('─', '', 1), 1),
                   text + '\nnew output', '❯', '─' * 80):
        assert parse_activity(broken, program) is None
    if program == 'dsh':
        assert parse_activity(text.replace('ctx 2.8% (28k/1.0m)', 'ctx 2.8% (28k/'), program) is None


def test_modal_controls_need_the_actual_modal_prompt():
    frame = (_CAPTURES / 'hermes-idle.txt').read_text()
    ask = '⚠️  Dangerous Command\nfixture\n↑/↓ to select, Enter to confirm\n'
    question = 'ExampleAgent needs your input\nfixture\ntype your answer and press Enter\n'
    assert parse_activity(ask + frame.replace('deepseek ❯', '⚠ ❯'), 'hermes') == 'ask'
    for symbol in ('? ❯', '✎ ❯'):
        assert parse_activity(question + frame.replace('deepseek ❯', symbol), 'hermes') == 'question'
    assert parse_activity(ask + frame.replace('deepseek ❯', '? ❯'), 'hermes') is None


@pytest.mark.parametrize('program', ['hermes', 'dsh'])
@pytest.mark.parametrize('state', ['wait', 'work'])
def test_panel_examples_in_output_do_not_override_current_input(program, state):
    file = program + ('-idle.txt' if state == 'wait' else '-work.txt')
    frame = (_CAPTURES / file).read_text()
    examples = [
        '⚠️  Dangerous Command\n↑/↓ to select, Enter to confirm\n',
        'ExampleAgent needs your input\ntype your answer and press Enter\n',
        'Awaiting approval · fixture\n↑/↓ select · Enter confirm · Esc reject\n',
        '▾  📋 Question 1/2\n↑/↓ select · Enter submit · Esc cancel · Ctrl+Q to fold / click the header\n',
    ]
    for example in examples:
        assert parse_activity(example + frame, program) == state


@pytest.mark.parametrize('program', ['dsh', 'hermes'])
def test_wait_and_work_transition_does_not_retain_a_previous_state(program):
    idle = (_CAPTURES / (program + '-idle.txt')).read_text()
    work = (_CAPTURES / (program + '-work.txt')).read_text()
    assert [parse_activity(text, program) for text in (idle, work, idle, 'hidden')] == [
        'wait', 'work', 'wait', None]


def _dsh_metrics_footer(above='', hint=''):
    # Native classic spinner above PromptInput; hover replaces the interrupt
    # hint with StatusMetrics below the context row (dsh-tui 0.16.1).
    return '\n'.join(filter(None, [above, '╭' + '─' * 60 + '╮',
        '⌸ ❯                                              ⛶',
        '╰' + '─' * 60 + '╯', '158k/1.0M 15.8%',
        'deepseek │ ctx 16% (158k/1.0m)', '·  ·  ·',
        'sys 981  pr 478  ast 6.7k  th 25k  tl 124k  free 842k', hint]))


@pytest.mark.parametrize('glyph', ['·', '•', '●'])
@pytest.mark.parametrize('duration', ['0s', '1m', '1h', '1h 2m 3s'])
def test_dsh_busy_metrics_footer_without_interrupt_hint(glyph, duration):
    text = _dsh_metrics_footer(f'{glyph} Searching… ({duration} · thought for 9s)')
    assert parse_activity(text, 'dsh') == 'work'
    assert parse_activity(text + '\nnew transcript' * 46, 'dsh') is None


def test_dsh_metrics_footer_requires_positive_current_activity():
    # A complete composer is shared by busy and idle states; missing evidence
    # must not imply wait, nor may arbitrary prose replace the actual spinner.
    assert parse_activity(_dsh_metrics_footer(), 'dsh') is None
    assert parse_activity(_dsh_metrics_footer('Searching…'), 'dsh') is None
    assert parse_activity(_dsh_metrics_footer(hint='? for shortcuts'), 'dsh') == 'wait'
    assert parse_activity(_dsh_metrics_footer(hint='? 查看快捷键'), 'dsh') == 'wait'
    busy = '● Searching… (5m 20s · thought for 9s)'
    assert parse_activity(_dsh_metrics_footer(busy) + '\nordinary output', 'dsh') is None
    assert parse_activity(_dsh_metrics_footer(busy + '\nnot the current spinner'), 'dsh') is None
    assert parse_activity(_dsh_metrics_footer(busy).replace('❯', ''), 'dsh') is None


def test_dsh_current_shortcut_supersedes_old_busy_and_modal_text():
    old = '● Searching… (5m 20s)\nAwaiting approval · bash\n'
    assert parse_activity(_dsh_metrics_footer(old, '? for shortcuts'), 'dsh') == 'wait'


@pytest.mark.parametrize('status', [
    '12s · thinking',
    '12s · ↓ ↓ 1.2k tokens · thought for 9s',
    '12s · ↑ ↑ 128 · ↓ 64 tokens',
])
def test_dsh_metrics_footer_classic_thinking_and_token_parts(status):
    assert parse_activity(_dsh_metrics_footer(f'● Thinking… ({status})'), 'dsh') == 'work'


_DSH_PANELS = json.loads((_CAPTURES / 'dsh-panels.json').read_text())['cases']


@pytest.mark.parametrize('case', _DSH_PANELS, ids=lambda case: case['id'])
def test_upstream_rendered_divider_and_panel_hint(case):
    # The fixture generator evaluates native Divider and source-extracted
    # title/hint expressions, including expanded ▾ and folded ▸ prefixes.
    assert parse_activity(case['text'], 'dsh') == case['state']
    styled = '\x1b[36m' + case['text'].replace('\n', '\x1b[0m\n\u3000\x1b[36m')
    assert parse_activity(styled, 'dsh') == case['state']
    lines = case['text'].splitlines()
    # Native Text widgets wrap long operation hints; word-boundary wrapping
    # must keep the observed operation and suppress the generic busy hint.
    lines[-2:-1] = textwrap.wrap(lines[-2], width=60)
    assert parse_activity('\n'.join(lines), 'dsh') == case['state']


@pytest.mark.parametrize('case', [case for case in _DSH_PANELS if 'approval' in case['id']], ids=lambda case: case['id'])
def test_long_approval_with_only_its_operation_footer_is_unknown(case):
    assert parse_activity(case['text'], 'dsh') == 'ask'
    long_panel = case['text'].replace('Fixture operation', '\n'.join(['wrapped command'] * 38))
    assert parse_activity(long_panel, 'dsh') is None
    clipped = long_panel.replace(' · Esc reject', '').replace(' · Esc 拒绝', '')
    assert parse_activity(clipped, 'dsh') is None
    # The same partial evidence must suppress work even after the title has
    # disappeared from the capture itself, not merely from the title window.
    assert parse_activity('\n'.join(long_panel.splitlines()[1:]), 'dsh') is None


@pytest.mark.parametrize('file,state', [
    ('hermes-question-real-footer.txt', 'question'),
    ('hermes-approval-real-footer.txt', 'ask'),
    ('hermes-long-approval-24-footer.txt', 'ask'),
    ('hermes-long-approval-60-footer.txt', 'ask'),
])
def test_real_hermes_modal_without_its_offscreen_title(file, state):
    # Exact captured footer rows; neither the panel title nor command echo is
    # needed when the current modal input and matching operation are visible.
    text = (_CAPTURES / file).read_text()
    assert parse_activity(text, 'hermes') == state
    rows = text.splitlines()
    assert parse_activity('\n'.join(rows[1:]), 'hermes') is None  # icon without hint
    assert parse_activity(rows[0], 'hermes') is None  # hint without frame
    top_rule = rows[-3]
    assert parse_activity(text.replace(top_rule, top_rule[:-1], 1), 'hermes') is None
    if state == 'ask':
        assert parse_activity(text.replace('⚠ ❯', '? ❯'), 'hermes') is None
    else:
        assert parse_activity(text.replace('? ❯', '⚠ ❯'), 'hermes') is None


@pytest.mark.parametrize('prompt,hint,state', [
    ('⚠ ❯', '↑/↓ to select, Enter to confirm', 'ask'),
    ('? ❯', '↑/↓ to select, Enter to lock, Tab next question', 'question'),
    ('✎ ❯', 'type your answer and press Enter', 'question'),
])
def test_hermes_modal_needs_matching_controls_even_without_a_title(prompt, hint, state):
    text = '\n'.join([hint, '─' * 80, prompt, '─' * 80])
    assert parse_activity(text, 'hermes') == state
    # Slash confirmation shares ⚠, but its different operation hint must not
    # become approval just because the state icon happens to match.
    changed = text.replace(hint, 'type 1/2/3, or ↑/↓ to select, Enter to confirm')
    assert parse_activity(changed, 'hermes') is None
    for ordinary, expected in [('fixture-profile ❯', 'wait'), ('☤ ❯', 'work')]:
        assert parse_activity(text.replace(prompt, ordinary), 'hermes') == expected
