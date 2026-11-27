"""Selection evidence for the terminal submission used by the Mail watcher."""

import itertools
import os
import runpy
import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(("prompt", "footer"), [
    ("> ", "? for shortcuts    Gemini 3.8 Flash · low"),
    ("> Auto mode: edits & safe commands auto-approved (shift+tab to cycle)", "auto · Gemini 3.8 Flash · low"),
])
def test_early_antigravity_footer_and_mcp_menu_are_recognized(prompt, footer):
    submission = runpy.run_path(str(ROOT / "bin/lib/pane_submission.py"))
    metadata = "/tmp/fixture.sock\t100\t%1\t200\t1\t0\t2\t1\tagy"
    border = "─" * 80
    blank = submission["frame"](metadata,
        [border, prompt, border, footer], "%1")
    assert blank.editor == "agy" and blank.empty
    menu = submission["frame"](metadata, [border, "> ", border, "MCP Servers",
        ">  ✓ orrery-mail  Tools: send_message", "Keyboard: ↑/↓ Navigate  enter Actions  esc Exit",
        "Gemini 3.8 Flash · low"], "%1")
    assert menu.slash_view == ("agy", "/mcp")


@pytest.mark.parametrize(('editor', 'body_length'), [
    ('agy', 111), ('claude', 111), ('codex', 111), ('dsh', 111), ('hermes', 111),
    ('hermes', 264), ('hermes', 2000),
])
@pytest.mark.parametrize('interleaving', [
    'left-in-editor', 'manual-enter', 'cleared', 'changed-draft', 'changed-inner-space',
])
def test_watcher_restarts_after_pasting_a_wrapped_notice(monkeypatch, tmp_path, editor, interleaving, body_length):
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    submit = submission['submit']
    monkeypatch.setitem(submit.__globals__, 'try_lock',
                        lambda view: os.open(tmp_path / 'pane.lock', os.O_RDWR | os.O_CREAT, 0o600))
    now = itertools.count()
    monkeypatch.setitem(submit.__globals__, 'time', SimpleNamespace(
        monotonic=lambda: next(now) * .1, sleep=lambda seconds: None))
    body = ('Several words wrapped in the editor. ' * 60)[:body_length]
    text = ('ORRERY Mail notification: message from FixtureSender [normal]: Ready. '
            'Body preview: ' + body +
            'Fetch inbox to read the rest.')
    draft = ''
    effects = []

    class WatcherStopped(BaseException):
        pass

    border = '─' * 80
    layouts = {
        'agy': ([border], '> ', [border, 'Gemini 4 Argon · high']),
        'claude': ([border], '❯ ', [border]),
        'codex': ([], '› ', ['', '  GPT-6.2-Sol high · Context 32% left']),
        'dsh': ([border], '│ ❯ ', [border]),
        'hermes': ([' ☤ fixture-model │ 2K/20K', border], '❯ ', [border]),
    }
    before, prompt, after = layouts[editor]

    def command(*args, input=None):
        nonlocal draft
        operation = args[0]
        output = ''
        if operation == 'display-message':
            body = textwrap.wrap(draft, 70) or ['']
            lines = [*before, prompt+body[0], *body[1:], *after]
            position = len(before)+len(body)-1
            column = len(body[-1]) + (len(prompt) if len(body)==1 else 0)
            metadata = f'/tmp/fixture.sock\t100\t%1\t200\t{position}\t0\t{column}\t1\tzsh'
            assert submission['frame'](metadata, lines, '%1').editor == editor
            output = '\n'.join([metadata,*lines,''])
        elif operation == 'load-buffer':
            assert input == text
            effects.append('load')
        elif operation == 'paste-buffer':
            effects.append('paste')
            draft = text
            if effects.count('paste') == 1:
                raise WatcherStopped
        elif operation == 'send-keys':
            assert args[-1] == 'C-m' and draft == text
            effects.append('enter')
            draft = ''
        else:
            assert operation == 'delete-buffer'
        return subprocess.CompletedProcess(args, 0, output, '')

    with pytest.raises(WatcherStopped):
        submit(command, lambda: True, '%1', text)
    if interleaving in ('manual-enter', 'cleared'):
        draft = ''
        if interleaving == 'manual-enter':
            effects.append('manual enter')
    elif interleaving == 'changed-draft':
        draft += ' Another draft line.'
    elif interleaving == 'changed-inner-space':
        draft = draft.replace('message from', 'messagefrom')
    reasons = []
    outcome = submit(command, lambda: True, '%1', text, on_wait=reasons.append)
    expected = ['load', 'paste']
    if interleaving == 'left-in-editor':
        assert outcome == 'accepted'
        expected.append('enter')
    elif interleaving in ('manual-enter', 'cleared'):
        assert outcome == 'accepted'
        if interleaving == 'manual-enter':
            expected.append('manual enter')
        expected.extend(('load', 'paste', 'enter'))
    else:
        assert outcome == 'refused' and reasons == ['editor_not_empty']
    assert effects == expected


@pytest.mark.parametrize('terminator', ['\x1b\\', '\x07'])
def test_watcher_compares_link_labels_without_osc8_targets(terminator):
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    metadata = '/tmp/fixture.sock\t100\t%1\t200\t1\t0\t16\t1\tzsh'
    start = f'\x1b]8;id=1;https://example.test/docs{terminator}'
    end = f'\x1b]8;;{terminator}'
    prefix = 'ORRERY Mail notification: message from FixtureSender [normal]: Docs.'
    lines = [f'› {prefix} {start}https://example.{end}',
             f'  {start}\x1b[32mtest/docs\x1b[0m{end}', '  Context 100% left']
    view = submission['frame'](metadata, lines, '%1')
    text = prefix + ' https://example.test/docs'
    assert view.draft == prefix + ' https://example.\n  test/docs'
    assert submission['ready'](view, text)
    assert submission['same_notice'](view, text)
    assert not submission['same_notice'](view, text.replace('/docs', '/other'))


@pytest.mark.parametrize('flags', [{'editor': None}, {'scrolled': True}, {'dsh_clipped': True}])
def test_watcher_does_not_resume_a_draft_it_cannot_fully_identify(flags):
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    text = 'ORRERY Mail notification: message from FixtureSender [normal]: Ready.'
    view = submission['Frame'](Path('/tmp/fixture.sock'), 100, '%1', 200, text, False,
                               **({'editor': 'dsh'} | flags))
    assert not submission['same_notice'](view, text)


def test_watcher_does_not_resume_when_draft_has_rows_below_the_cursor():
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    metadata = '/tmp/fixture.sock\t100\t%1\t200\t0\t0\t20\t1\tzsh'
    text = 'ORRERY Mail notification: message from FixtureSender [normal]: Ready.'
    lines = [f'› {text}', '  A separate draft line below the cursor.', '  Context 100% left']
    view = submission['frame'](metadata, lines, '%1')
    assert view.draft == text and view.draft_continues
    assert not submission['same_notice'](view, text)


@pytest.mark.parametrize('width', [56, 100])
def test_watcher_confirms_a_wrapped_hermes_paste_card(width):
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    notice = 'ORRERY Mail notification: message from FixtureSender [normal]: ' + 'あ' * 2000
    card = '❯ [Pasted text #1: 1 lines → /private/tmp/fixture-hermes-home/pastes/paste_1_140243.txt]'
    rows = ['☤ fixture · -- · 4s', '─' * width]
    rows.extend(card[offset:offset + width] for offset in range(0, len(card), width))
    cursor_row = len(rows) - 1
    cursor_col = len(rows[-1])
    metadata = f'/tmp/fixture.sock\t100\t%1\t200\t{cursor_row}\t0\t{cursor_col}\t1\tzsh'
    view = submission['frame'](metadata, rows, '%1')
    assert view.hermes
    assert submission['ready'](view, notice)
    assert not submission['ready'](view, notice + '\nA second line')


def test_watcher_confirms_a_long_dsh_notice_from_its_visible_end():
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    body = ('Several words wrapped in the dsh editor. ' * 60)[:2000]
    text = ('ORRERY Mail notification: message from FixtureSender [normal]: Ready. '
            'Body (complete; no inbox fetch needed): ' + body)
    visible = textwrap.wrap(text, 52)[-5:]
    border = '─' * 56
    rows = [border, '⌸ ❯ ' + visible[0], *visible[1:], border]
    metadata = '/tmp/fixture.sock\t100\t%1\t200\t5\t0\t46\t1\tdsh'
    view = submission['frame'](metadata, rows, '%1')
    assert view.dsh_clipped and len(view.draft) < len(text)
    assert submission['ready'](view, text)
    assert not submission['ready'](view, text + ' Different ending.')


@pytest.mark.parametrize(('visible_chars', 'accepted'), [(11, False), (20, True)])
def test_watcher_confirms_a_wide_character_suffix_in_dsh_welcome(visible_chars, accepted):
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    body = ('日本語の通知本文と折り返しを確かめます。' * 100)[:2000]
    notice = 'ORRERY Mail notification: message from FixtureSender [normal]: ' + body
    border = '─' * 56
    rows = [border, '│ ❯ ' + body[-visible_chars:] + '  │', border]
    cursor_col = 4 + 2 * visible_chars
    metadata = f'/tmp/fixture.sock\t100\t%1\t200\t1\t0\t{cursor_col}\t1\tdsh'
    view = submission['frame'](metadata, rows, '%1')
    assert view.dsh_clipped
    assert submission['ready'](view, notice) is accepted
    assert not submission['ready'](view, notice + ' Different ending.')


@pytest.mark.parametrize('editor,editor_text,selection,accepted', [
    ('claude', '/log', '/log', True),
    ('claude', '/log', '/login', False),
    ('claude', '/log', None, True),
    ('claude', '/log note', None, True),
    ('claude-wrapped', '/compact', '/compact', True),
    ('claude-wrapped', '/compact', '/compact-later', False),
    ('codex', '/compact', '/compact', True),
    ('codex', '/comp', '/compact', False),
    ('codex', '/compact', None, True),
    ('codex', 'A plain notice', None, True),
])
def test_watcher_submits_matching_or_picker_free_slash_draft(monkeypatch, editor, editor_text, selection, accepted):
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    submit = submission['submit']
    paste = sent = False
    monkeypatch.setitem(submit.__globals__, 'try_lock', lambda view: os.open(os.devnull, os.O_RDONLY))

    def command(*args, input=None):
        nonlocal paste, sent
        screen = ''
        if args[0] == 'display-message':
            draft = editor_text if paste and not sent else ''
            active = paste and not sent and selection
            if editor.startswith('claude'):
                options = [f'  ❯ {selection}  selected', '    /other  unselected'] if active else []
                if active and editor == 'claude-wrapped':
                    # A narrow pane wraps long descriptions under their option.
                    options[1:1] = ['              description that wraps']
                    options.append('              another wrapped description')
                lines = [*options, '────────', f'❯ {draft}', '────────']
                cursor = len(options) + 1
            else:
                options = [f'\x1b[1;7m› {selection}  selected\x1b[0m', ''] if active else []
                lines = [*options, f'› {draft}', '  Context 100% left']
                cursor = len(options)
            command_name = 'claude' if editor.startswith('claude') else editor
            screen = (f'/tmp/fixture.sock\t11\t%1\t12\t{cursor}\t0\t{len(draft)+2}\t1\t{command_name}\n'
                      + '\n'.join(lines) + '\n')
        elif args[0] == 'load-buffer':
            assert input == editor_text
        elif args[0] == 'paste-buffer':
            paste = True
        elif args[0] == 'send-keys':
            assert not sent and args[-1] == 'C-m'
            sent = True
        else:
            assert args[0] == 'delete-buffer'
        return subprocess.CompletedProcess(args, 0, screen, '')

    now = itertools.count()
    monkeypatch.setitem(submit.__globals__, 'time', SimpleNamespace(
        monotonic=lambda: next(now) * .1, sleep=lambda seconds: None))
    result = submit(command, lambda: True, '%1', editor_text, deadline=12)
    assert paste and sent is accepted
    assert result == ('accepted' if accepted else 'unknown')


@pytest.mark.parametrize('styled,column,empty', [
    # Claude suggests a next prompt after a turn: dim text at the caret.
    ('\x1b[39m❯\xa0\x1b[2mrun the focused tests again\x1b[0m', 2, True),
    # A typed draft is never dim, wherever the caret is.
    ('\x1b[39m❯\xa0run the focused tests again', 2, False),
    ('\x1b[39m❯\xa0run the focused tests again', 29, False),
    # Typed text followed by a dim completion is still a draft.
    ('\x1b[39m❯\xa0run\x1b[2m the focused tests again\x1b[0m', 2, False),
])
def test_watcher_treats_claude_prompt_suggestion_as_empty(styled, column, empty):
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    border = '─' * 24
    lines = [border, styled, border, '  fixture | Opus | ctx: 22% used']
    metadata = f'/tmp/fixture.sock\t100\t%1\t200\t1\t0\t{column}\t1\tclaude'
    assert submission['frame'](metadata, lines, '%1').empty is empty


@pytest.mark.parametrize('busy', [False, True])
def test_watcher_retries_its_own_folded_codex_notice_after_a_transient_clear(monkeypatch, tmp_path, busy):
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    submit = submission['submit']
    lock = tmp_path / 'pane.lock'
    monkeypatch.setitem(submit.__globals__, 'try_lock',
                        lambda view: os.open(lock, os.O_RDWR | os.O_CREAT, 0o600))
    now = itertools.count()
    monkeypatch.setitem(submit.__globals__, 'time', SimpleNamespace(
        monotonic=lambda: next(now) * .03, sleep=lambda _: None))
    notice = 'ORRERY Mail notification: message from Fixture [normal]: ' + '日本語🧪' * 300
    draft = f'\x1b[38;5;6m[Pasted Content {len(notice)} chars]\x1b[39m'
    idle = 'gpt-5.4 default · fixture'
    queue = '  \x1b[1mtab\x1b[0m to queue message          100% context left'
    screen = draft
    transient = False
    consume = False
    effects = []
    reasons = []

    def command(*args, input=None):
        nonlocal screen, transient
        operation = args[0]
        if operation == 'display-message':
            current = '' if transient else screen
            transient = False
            footer = queue if current and busy else idle
            lines = [f'\x1b[1m›\x1b[0m {current}', '', footer]
            column = 2 + len(submission['SGR'].sub('', current))
            meta = f'/tmp/fixture.sock\t100\t%1\t200\t0\t0\t{column}\t1\tcodex-code-mode-host'
            return subprocess.CompletedProcess(args, 0, '\n'.join([meta, *lines, '']), '')
        effects.append(operation if operation != 'send-keys' else args[-1])
        if operation == 'load-buffer':
            assert input == notice
        elif operation == 'paste-buffer':
            screen = draft
        elif operation == 'send-keys':
            if consume:
                screen = ''
            else:
                transient = True
        return subprocess.CompletedProcess(args, 0, '', '')

    # A pre-existing capsule, even with the exact length, has no owner receipt.
    assert submit(command, lambda: True, '%1', notice, on_wait=reasons.append) == 'refused'
    assert effects == [] and reasons == ['editor_not_empty']

    screen = ''
    key = 'Tab' if busy else 'C-m'
    assert submit(command, lambda: True, '%1', notice, on_wait=reasons.append) == 'unknown'
    assert effects == ['load-buffer', 'paste-buffer', key, 'delete-buffer']
    assert reasons[-1] == 'enter_not_confirmed' and screen == draft

    effects.clear()
    wrong_notice = notice.replace('Fixture', 'Another')
    assert len(wrong_notice) == len(notice)
    assert submit(command, lambda: True, '%1', wrong_notice, on_wait=reasons.append) == 'refused'
    assert effects == [] and reasons[-1] == 'editor_not_empty'

    consume = True
    assert submit(command, lambda: True, '%1', notice) == 'accepted'
    assert effects == [key] and screen == '' and not lock.read_bytes()


@pytest.mark.parametrize('editor', ['agy', 'claude', 'codex', 'dsh', 'hermes'])
def test_watcher_requires_a_stable_clear_after_the_submit_key(monkeypatch, tmp_path, editor):
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    submit = submission['submit']
    scope = submit.__globals__
    monkeypatch.setitem(scope, 'try_lock',
                        lambda view: os.open(tmp_path / 'pane.lock', os.O_RDWR | os.O_CREAT, 0o600))
    ticks = itertools.count()
    monkeypatch.setitem(scope, 'time', SimpleNamespace(
        monotonic=lambda: next(ticks) * .05, sleep=lambda _: None))
    tail = 'A distinct notification ending for the redraw fixture.'
    notice = 'ORRERY Mail notification: message from Fixture [normal]: ' + tail
    identity = (Path('/tmp/fixture.sock'), 100, '%1', 200)
    frame = submission['Frame']
    empty = frame(*identity, '', False, editor=editor, bracketed=True)
    pending = frame(*identity, notice, False, editor=editor, bracketed=True)
    if editor == 'agy':
        border = '─' * 80
        rows = [border, '\x1b[94m>\x1b[39m \x1b[90m↑ 7 more lines\x1b[39m',
                '  ' + tail, border, 'auto · Gemini 4 Neon · xhigh']
        metadata = f'/tmp/fixture.sock\t100\t%1\t200\t2\t0\t{len(tail)+2}\t1\tagy'
        pending = submission['frame'](metadata, rows, '%1')
        assert pending.scrolled
    after_key = iter(())
    consumed = False
    keys = []
    reasons = []

    def capture(command, pane):
        return next(after_key, empty if consumed and keys else pending)

    def command(*args):
        nonlocal after_key
        assert args[0] == 'send-keys'
        keys.append(args[-1])
        after_key = iter([empty, pending] if not consumed else [empty])
        return subprocess.CompletedProcess(args, 0, '', '')

    monkeypatch.setitem(scope, 'capture', capture)
    assert submit(command, lambda: True, '%1', notice, on_wait=reasons.append) == 'unknown'
    assert keys == ['C-m'] and reasons == ['enter_not_confirmed']
    keys.clear()
    consumed = True
    assert submit(command, lambda: True, '%1', notice) == 'accepted'
    assert keys == ['C-m']


@pytest.mark.parametrize('editor', ['agy', 'claude', 'codex', 'dsh', 'hermes'])
def test_retry_does_not_paste_again_after_an_operator_clears_the_owned_draft(
    monkeypatch, tmp_path, editor,
):
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    submit = submission['submit']
    scope = submit.__globals__
    monkeypatch.setitem(scope, 'try_lock',
                        lambda view: os.open(tmp_path / 'pane.lock', os.O_RDWR | os.O_CREAT, 0o600))
    frames = itertools.count()
    monkeypatch.setitem(scope, 'time', SimpleNamespace(
        monotonic=lambda: next(frames) * .05, sleep=lambda _: None))
    adapter = runpy.run_path(str(ROOT / 'bin/lib/runtime_tmux_consumer.py'))
    handoff = adapter['_NotificationHandoff']
    monkeypatch.setenv('AGENTSTACK_RUNTIME_DIR', str(tmp_path))
    signal = tmp_path / '41428.signal'
    signal.write_text('{}')
    st = signal.stat()
    signal_id = f'{st.st_dev}:{st.st_ino}:{st.st_mtime_ns}:{st.st_size}'
    notice = 'ORRERY Mail notification: message from Fixture [normal]: Check the fixture scope.'
    identity = (Path('/tmp/fixture.sock'), 100, '%1', 200)
    frame = submission['Frame']
    draft = ''
    calls = []
    reasons = []

    def capture(command, pane):
        return frame(*identity, draft, False, editor=editor, bracketed=True)

    def command(*args, input=None):
        nonlocal draft
        calls.append(args[0])
        if args[0] == 'paste-buffer':
            assert (tmp_path / 'notify-state.json').is_file()
            draft = notice
        return subprocess.CompletedProcess(args, 0, '', '')

    def attempt(key='Fixture:41428'):
        current = handoff(str(signal), signal_id, key)
        return submit(command, current.current, '%1', notice, on_wait=reasons.append,
                      claim_handoff=current.claim)

    monkeypatch.setitem(scope, 'capture', capture)
    assert attempt() == 'unknown'
    assert reasons[-1] == 'enter_not_confirmed'
    assert 'paste-buffer' in calls

    calls.clear()
    draft = ''  # The operator submitted or cleared the draft between watcher runs.
    assert attempt() == 'unknown' and reasons[-1] == 'prior_delivery_unconfirmed'
    assert 'paste-buffer' not in calls and 'send-keys' not in calls

    calls.clear()
    assert attempt('Fixture:41429') == 'unknown' and reasons[-1] == 'enter_not_confirmed'
    assert 'paste-buffer' in calls  # Identical text with its own signal key remains deliverable.

    calls.clear()
    signal.unlink()  # A fetch or a read can consume this hint during delivery.
    assert attempt() == 'refused'
    assert not calls


def test_watcher_reads_the_end_of_a_scrolled_claude_draft():
    # A tall draft scrolls inside Claude's frame and hides the prompt glyph.
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    rows = ['\x1b[38;5;244m───\x1b[39m \x1b[2m↑ 3 more\x1b[0m \x1b[38;5;244m' + '─' * 20,
            '\x1b[39m  the middle of a long notification that wraps',
            '  and ends here. Fetch inbox to read the rest.',
            '\x1b[38;5;244m' + '─' * 24,
            '  fixture | Opus | ctx: 22% used']
    metadata = '/tmp/fixture.sock\t100\t%1\t200\t2\t0\t48\t1\tclaude'
    view = submission['frame'](metadata, rows, '%1')
    assert view.scrolled and not view.empty
    text = ('ORRERY Mail notification: the start is hidden above, then '
            'the middle of a long notification that wraps and ends here. '
            'Fetch inbox to read the rest.')
    assert submission['ready'](view, text)
    assert not submission['ready'](view, 'A different prompt that ends elsewhere.')


def test_codex_reads_the_terminal_scroll_marker_without_stripping_typed_arrows():
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    footer = ['', '  GPT-6.1-Sol default · ~']
    metadata = '/tmp/fixture.sock\t100\t%1\t200\t2\t0\t30\t1\tzsh'
    text = ('ORRERY Mail notification: message from FixtureSender [normal]: Ready. Body preview: '
            'Segment 5 has stable identifying text for this specific mail. Fetch inbox to read the rest.')
    rows = ['\x1b[1m›\x1b[0m Segment 5 has stable identifying text for this      \x1b[2m↑',
            '\x1b[0m  specific mail.', '  Fetch inbox to read the rest.', *footer]
    view = submission['frame'](metadata, rows, '%1')
    assert view.editor == 'codex' and view.scrolled and not view.draft_continues
    assert '↑' not in view.draft and submission['ready'](view, text)
    assert submission['same_notice'](view, text)

    rows[0] = rows[0].replace('\x1b[2m↑', '↑')
    typed = submission['frame'](metadata, rows, '%1')
    assert not typed.scrolled and '↑' in typed.draft

    rows[0] = rows[0].replace('↑', '\x1b[2m↑')
    rows[2] += '\x1b[2m↓'
    partial = submission['frame'](metadata, rows, '%1')
    assert partial.scrolled and partial.draft_continues
    assert not submission['same_notice'](partial, text)
    assert not submission['ready'](partial, text)


def test_codex_resumes_captured_notice_with_one_space_before_scroll_arrow():
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    # Captured Codex layout with the mail body and local path replaced.
    # The right-edge scroll marker has only one space after the inline newline.
    rows = [
        '\x1b[1m›\x1b[0m\x1b[48;2;30;30;30m 選択モードに切り替えると、一覧に確認用の項目が並びます。 ⏎ \x1b[2m↑',
        '  - 一覧の表示を確認する ⏎ - 画像：/Users/',
        '  fixture/project/.orrery-',
        '  input-images/capture.png ⏎  ⏎ 左側の一覧は、mail-',
        '  arrival の修正の後でも表示されます。 ⏎ - 結果を報告する',
        '  ⏎ - 変更はローカルに保存する',
        '',
        '  GPT-6.2-Sol high · Context 80% left',
    ]
    metadata = '/tmp/fixture.sock\t100\t%1\t200\t5\t0\t16\t1\tzsh'
    notice = (
        'ORRERY Mail notification: message from FixtureSender [normal]: 一覧の確認。 '
        'Body (complete; no inbox fetch needed): '
        '選択モードに切り替えると、一覧に確認用の項目が並びます。 ⏎ '
        '- 一覧の表示を確認する ⏎ - 画像：/Users/fixture/project/.orrery-input-images/capture.png '
        '⏎  ⏎ 左側の一覧は、mail-arrival の修正の後でも表示されます。 ⏎ - 結果を報告する '
        '⏎ - 変更はローカルに保存する'
    )
    view = submission['frame'](metadata, rows, '%1')
    assert view.editor == 'codex' and view.scrolled and not view.draft_continues
    assert submission['ready'](view, notice)
    assert submission['same_notice'](view, notice)
    assert not submission['same_notice'](view, notice.replace('capture.png', 'other.png'))


def test_codex_resumes_captured_notice_with_scroll_arrow_attached_to_text():
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    # Captured Codex layout with the body replaced. The arrow directly follows
    # the last visible character of the first row.
    rows = [
        '\x1b[1m›\x1b[0m\x1b[48;2;30;30;30m 操作を撮り直す. Body (complete; no inbox fetch\x1b[2m↑',
        '  needed): 試験では、画面の切り',
        '  替えと確認を順に撮ってください。 ⏎ - 動画はスマホでも読めるようにします。',
        '  ⏎ - 完了したら結果を報告してください。',
        '',
        '  GPT-6.2-Sol high · Context 80% left',
        '                                    ⚠ 2 warnings · f2 to view',
    ]
    metadata = '/tmp/fixture.sock\t100\t%1\t200\t3\t0\t40\t1\tzsh'
    notice = (
        'ORRERY Mail notification: message from FixtureSender [normal]: '
        '操作を撮り直す. Body (complete; no inbox fetch needed): 試験では、画面の切り'
        '替えと確認を順に撮ってください。 ⏎ - 動画はスマホでも読めるようにします。 '
        '⏎ - 完了したら結果を報告してください。'
    )
    view = submission['frame'](metadata, rows, '%1')
    assert view.scrolled and '↑' not in view.draft
    assert submission['ready'](view, notice)
    assert submission['same_notice'](view, notice)
    assert not submission['same_notice'](view, notice.replace('スマホ', 'デスクトップ'))


@pytest.mark.parametrize('editor', ['agy', 'claude', 'codex', 'dsh', 'hermes'])
@pytest.mark.parametrize('case', ['same', 'changed', 'generic-trailer'])
def test_watcher_retries_a_scrolled_notice_from_its_visible_tail(monkeypatch, tmp_path, editor, case):
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    submit = submission['submit']
    frames = submit.__globals__
    monkeypatch.setitem(frames, 'try_lock',
                        lambda view: os.open(tmp_path / 'pane.lock', os.O_RDWR | os.O_CREAT, 0o600))
    # A distinct section remains visible before the shared preview trailer.
    trailer = ' ... Fetch inbox to read the rest.'
    body = '日本語の通知本文と折り返しを確かめます。Fixture 41097 shows the right ending.'
    notice = ('ORRERY Mail notification: message from FixtureSender [normal]: Ready. Body preview: '
              + body + trailer)
    draft = '本語の通知本文と折り返しを確かめます。\nFixture 41097 shows the right ending.' + trailer
    if case == 'changed':
        draft = draft.replace('right ending', 'wrong ending')
    elif case == 'generic-trailer':
        draft = trailer.strip()
    frame = submission['Frame']
    identity = (Path('/tmp/fixture.sock'), 100, '%1', 200)
    pending = frame(*identity, draft, False, editor=editor, bracketed=True,
                    scrolled=editor != 'dsh', dsh_clipped=editor == 'dsh')
    empty = frame(*identity, '', False, editor=editor, bracketed=True)
    operations = []
    reasons = []
    ticks = itertools.count()
    monkeypatch.setitem(frames, 'time', SimpleNamespace(
        monotonic=lambda: next(ticks) * .01, sleep=lambda _: None))
    monkeypatch.setitem(frames, 'capture', lambda command, pane: empty if operations else pending)

    def command(*args):
        operations.append(args)
        return subprocess.CompletedProcess(args, 0, '', '')

    result = submit(command, lambda: True, '%1', notice, on_wait=reasons.append)
    if case == 'same':
        assert result == 'accepted' and operations == [('send-keys', '-t', '%1', 'C-m')]
        assert not reasons
    else:
        assert result == 'refused' and not operations and reasons == ['editor_not_empty']


@pytest.mark.parametrize('case', [
    'fresh', 'retry', 'different', 'typed-marker', 'cursor-inside', 'more-below', 'no-agy-footer',
])
def test_watcher_submits_a_scrolled_agy_mail_notice(monkeypatch, case):
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    submit = submission['submit']
    monkeypatch.setitem(submit.__globals__, 'try_lock', lambda view: os.open(os.devnull, os.O_RDONLY))
    ticks = itertools.count()
    monkeypatch.setitem(submit.__globals__, 'time', SimpleNamespace(
        monotonic=lambda: next(ticks) * .01, sleep=lambda _: None))
    border = '\x1b[90m' + '─' * 80
    footer = '\x1b[39mauto · Gemini 4 Neon (XHigh) · xhigh'
    if case == 'retry':
        footer = '\x1b[39m? for shortcuts  Gemini 4 Neon (XHigh) · xhigh'
    idle = [border, '\x1b[94m>\x1b[39m ', border, footer]
    marker = '\x1b[94m>\x1b[39m \x1b[90m↑ 7 more lines\x1b[39m'
    tail = [
        '  保存）、そのあと別の画面で開いて内容を確認します。',
        '  最後に識別用の印を記録します。',
    ]
    notice = ('ORRERY Mail notification: message from FixtureSender [normal]: 作業の確認。 '
              'Body (complete; no inbox fetch needed): 見本を作成（' + ' '.join(s.strip() for s in tail))
    column = 2 + sum(2 if ord(c) > 127 else 1 for c in tail[-1].strip())
    if case == 'different':
        tail[-1] = tail[-1].replace('印', '値')
    elif case == 'typed-marker':
        marker = '> ↑ 7 more lines'
    elif case == 'cursor-inside':
        column -= 2
    after = [border, footer]
    if case == 'more-below':
        after.insert(0, '  \x1b[90m↓ 2 more lines\x1b[39m')
    elif case == 'no-agy-footer':
        after[1] = 'Unrelated status'
    scrolled = [border, marker, *tail, *after]
    screen = idle if case == 'fresh' else scrolled
    effects = []

    def tmux(*args, input=None):
        nonlocal screen
        operation = args[0]
        if operation == 'display-message':
            position = '1\t0\t2' if screen is idle else f'3\t0\t{column}'
            metadata = f'/tmp/fixture.sock\t100\t%1\t200\t{position}\t1\tagy'
            return subprocess.CompletedProcess(args, 0, '\n'.join([metadata, *screen, '']), '')
        effects.append(operation)
        if operation == 'load-buffer':
            assert input == notice
        elif operation == 'paste-buffer':
            screen = scrolled
        elif operation == 'send-keys':
            assert args[-1] == 'C-m'
            screen = idle
        return subprocess.CompletedProcess(args, 0, '', '')

    result = submit(tmux, lambda: True, '%1', notice)
    if case == 'fresh':
        assert result == 'accepted' and effects == ['load-buffer', 'paste-buffer', 'send-keys', 'delete-buffer']
    elif case == 'retry':
        assert result == 'accepted' and effects == ['send-keys']
    else:
        assert result == 'refused' and not effects


@pytest.mark.parametrize('case', ['fresh', 'retry', 'different', 'cursor-inside'])
def test_watcher_submits_a_classic_hermes_notice_with_its_prompt_scrolled_off(monkeypatch, case):
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    submit = submission['submit']
    monkeypatch.setitem(submit.__globals__, 'try_lock', lambda view: os.open(os.devnull, os.O_RDONLY))
    ticks = itertools.count()
    monkeypatch.setitem(submit.__globals__, 'time', SimpleNamespace(
        monotonic=lambda: next(ticks) * .01, sleep=lambda _: None))
    border = '─' * 80
    status = ' ☤ fixture-model │ ~18.6K/1M │ [░░░░░░░░░░] ~2% ─ Fetch canonical task'
    idle = [status, border, '❯ ', border]
    tail = [
        'ox fetch needed): This is a synthetic initial task.',
        'Report the price and arrival date of the blue bicycle.',
        'Add each result on its own row, keeping the file unchanged.',
        'Use the named inventory in the current fixture.',
        'If any details are missing, record what was unavailable.',
        'Finish by confirming the result to the fixture parent.',
        'The final comparison goes in the reply only.',
        'Unique marker: HERMES-FIXTURE-4917.',
    ]
    notice = ('ORRERY Mail notification: message from FixtureSender [normal]: Initial task. '
              'Body (complete; no inb' + ' '.join(tail))
    draft = tail[:]
    if case == 'different':
        draft[-1] = 'Unique marker: HERMES-FIXTURE-4918.'
    screen = idle if case == 'fresh' else [status, border, *draft, border]
    effects = []
    reasons = []

    def tmux(*args, input=None):
        nonlocal screen
        operation = args[0]
        if operation == 'display-message':
            row = len(screen) - 2
            column = (2 if screen is idle or case == 'cursor-inside' else len(draft[-1]))
            metadata = f'/tmp/fixture.sock\t100\t%1\t200\t{row}\t0\t{column}\t\tbash'
            return subprocess.CompletedProcess(args, 0, '\n'.join([metadata, *screen, '']), '')
        effects.append(operation)
        if operation == 'load-buffer':
            assert input == notice
        elif operation == 'paste-buffer':
            screen = [status, border, *draft, border]
        elif operation == 'send-keys':
            assert args[-1] == 'C-m'
            screen = idle
        return subprocess.CompletedProcess(args, 0, '', '')

    result = submit(tmux, lambda: True, '%1', notice, on_wait=reasons.append)
    if case == 'fresh':
        assert result == 'accepted' and not reasons
        assert effects == ['load-buffer', 'paste-buffer', 'send-keys', 'delete-buffer']
    elif case == 'retry':
        assert result == 'accepted' and not reasons
        assert effects == ['send-keys']
    else:
        assert result == 'refused' and not effects
        assert reasons == ['editor_not_empty']


def test_expired_submit_after_load_still_deletes_its_buffer(monkeypatch):
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    submit = submission['submit']
    monkeypatch.setitem(submit.__globals__, 'try_lock', lambda view: os.open(os.devnull, os.O_RDONLY))
    now = 0
    loaded = []
    deleted = []
    reasons = []

    def command(*args, input=None):
        nonlocal now
        screen = ''
        if args[0] == 'display-message':
            screen = '/tmp/fixture.sock\t100\t%1\t200\t0\t0\t2\t1\tclaude\n❯ '
        elif args[0] == 'load-buffer':
            loaded.append(args[2])
            now = 6
        else:
            assert args[0] == 'delete-buffer'
            deleted.append(args[2])
        return subprocess.CompletedProcess(args, 0, screen, '')

    monkeypatch.setitem(submit.__globals__, 'time', SimpleNamespace(monotonic=lambda: now))
    assert submit(command, lambda: True, '%1', 'Fixture notice', on_wait=reasons.append) == 'refused'
    assert reasons == ['submission_deadline']
    assert len(loaded) == 1 and loaded == deleted
