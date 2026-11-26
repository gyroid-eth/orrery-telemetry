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


@pytest.mark.parametrize('editor', ['agy', 'claude', 'codex', 'dsh', 'hermes'])
@pytest.mark.parametrize('interleaving', [
    'left-in-editor', 'manual-enter', 'cleared', 'changed-draft', 'changed-inner-space',
])
def test_watcher_restarts_after_pasting_a_wrapped_notice(monkeypatch, editor, interleaving):
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    submit = submission['submit']
    monkeypatch.setitem(submit.__globals__, 'try_lock', lambda view: os.open(os.devnull, os.O_RDONLY))
    now = itertools.count()
    monkeypatch.setitem(submit.__globals__, 'time', SimpleNamespace(
        monotonic=lambda: next(now) * .1, sleep=lambda seconds: None))
    text = ('ORRERY Mail notification: message from FixtureSender [normal]: Ready. '
            'Body preview: ' + 'Several words wrapped in the editor. ' * 3 +
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
        monotonic=lambda: next(now), sleep=lambda seconds: None))
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
