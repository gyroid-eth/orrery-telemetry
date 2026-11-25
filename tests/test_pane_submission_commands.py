"""Selection evidence for the terminal submission used by the Mail watcher."""

import itertools
import os
import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('editor,editor_text,selection,accepted', [
    ('claude', '/log', '/log', True),
    ('claude', '/log', '/login', False),
    ('claude', '/log', None, False),
    ('claude', '/log note', None, True),
    ('claude-wrapped', '/compact', '/compact', True),
    ('claude-wrapped', '/compact', '/compact-later', False),
    ('codex', '/compact', '/compact', True),
    ('codex', '/comp', '/compact', False),
    ('codex', '/compact', None, False),
    ('codex', 'A plain notice', None, True),
])
def test_watcher_submits_only_proven_slash_completion(monkeypatch, editor, editor_text, selection, accepted):
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
    assert not submission['same_notice'](view, text)


def test_a_preexisting_notice_needs_an_exact_complete_visible_editor():
    submission = runpy.run_path(str(ROOT / 'bin/lib/pane_submission.py'))
    line = 'ORRERY Mail notification: message from Sender. Please read it.'
    metadata = '/tmp/fixture.sock\t100\t%1\t200\t2\t0\t15\t1\tclaude'
    border = '─' * 24
    view = submission['frame'](metadata, [border, '❯ ORRERY Mail notification: message',
                                        '  from Sender. Please read it.', border], '%1')
    assert submission['same_notice'](view, line)
    assert not submission['same_notice'](view, line.replace('from Sender', 'from  Sender'))
    view = submission['frame'](metadata, [border, '❯ ORRERY Mail notification: message',
                                        '  from Sender. Please read it.', ' extra draft text', border], '%1')
    assert not submission['same_notice'](view, line)
