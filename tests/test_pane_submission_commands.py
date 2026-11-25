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
            if editor == 'claude':
                options = [f'  ❯ {selection}  selected', '    /other  unselected'] if active else []
                lines = [*options, '────────', f'❯ {draft}', '────────']
                cursor = len(options) + 1
            else:
                options = [f'\x1b[1;7m› {selection}  selected\x1b[0m', ''] if active else []
                lines = [*options, f'› {draft}', '  Context 100% left']
                cursor = len(options)
            screen = (f'/tmp/fixture.sock\t11\t%1\t12\t{cursor}\t0\t{len(draft)+2}\t1\t{editor}\n'
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
