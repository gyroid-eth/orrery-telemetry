"""Provider prompt recognition for the guarded tmux input protocol."""

from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest


submission = runpy.run_path(str(Path(__file__).resolve().parents[1] / "bin/lib/pane_submission.py"))
frame = submission["frame"]
BORDER = "─" * 80
METADATA = "/unused/tmux.sock\t100\t%1\t200\t1\t0\t2\t1\tpython"


@pytest.mark.parametrize(
    ("lines", "draft"),
    [
        ([BORDER, "> ", BORDER, "Gemini 4 Neon (Low) · low"], ""),
        ([BORDER, "> Keep this draft", BORDER, "Gemini 4 Neon (Low) · low"], "Keep this draft"),
        ([BORDER, "> ", BORDER, "Another model · low"], None),
        ([BORDER, "> ", BORDER, "Gemini 4 Neon (Low) · launching"], None),
        (["", "> ", BORDER, "Gemini 4 Neon (Low) · low"], None),
    ],
)
def test_antigravity_model_only_footer_requires_the_framed_editor(lines, draft):
    assert frame(METADATA, lines, "%1").draft == draft


HERMES_IDLE_HINT = "Ask anything, or type / for commands…"
HERMES_BUSY_HINT = "msg=interrupt · /queue · /bg · /steer · Ctrl+C cancel"


@pytest.mark.parametrize(
    ("prompt", "timer", "draft", "busy"),
    [
        (f"\x1b[38;5;230m❯ \x1b[3m\x1b[38;5;136m{HERMES_IDLE_HINT}\x1b[0m", "16m", "", False),
        (f"❯ {HERMES_IDLE_HINT}", "16m", HERMES_IDLE_HINT, False),
        ("❯ Keep this draft", "16m", "Keep this draft", False),
        (f"\x1b[3m☤ {HERMES_BUSY_HINT}\x1b[0m", "16m", "", True),
        (f"\x1b[3m☤ \x1b[38;5;136m{HERMES_BUSY_HINT}\x1b[0m", "⏱ 4s", "", True),
        ("☤ ❯ \x1b[3mcli.tui.placeholder_busy\x1b[0m", "⏱ 4s", "", True),
        (f"\x1b[3m☤ \x1b[23m{HERMES_BUSY_HINT}", "⏱ 4s", HERMES_BUSY_HINT, True),
    ],
)
def test_hermes_compact_composer_without_lower_rule(prompt, timer, draft, busy):
    status = f"\x1b[48;5;234m ☤ fixture-model · ~26% · {timer}\x1b[0m"
    column = 4 if "☤ ❯" in prompt else 2
    metadata = f"/unused/tmux.sock\t100\t%1\t200\t2\t0\t{column}\t1\tzsh"
    lines = [status, "─" * 56, prompt, ""]
    view = frame(metadata, lines, "%1")
    assert view.hermes and view.editor == "hermes"
    assert view.draft == draft
    assert view.empty is (draft == "")
    assert view.hermes_busy is busy
    assert view.running is (busy and timer.startswith("⏱"))
    assert view.allows_paste("ORRERY Mail notification: fixture") is not busy


def test_hermes_compact_hint_requires_an_adjacent_status_bar():
    metadata = "/unused/tmux.sock\t100\t%1\t200\t2\t0\t2\t1\tzsh"
    lines = ["Earlier output mentioned ☤ Hermes", "─" * 56, f"❯ \x1b[3m{HERMES_IDLE_HINT}\x1b[0m"]
    view = frame(metadata, lines, "%1")
    assert not view.hermes and view.draft == HERMES_IDLE_HINT


@pytest.mark.parametrize(('editor', 'rows', 'command', 'argv'), [
    ('claude', [BORDER, '❯ ', BORDER], '2.1.332', '/home/demo/.local/share/claude/versions/2.1.332'),
    ('codex', ['', '› '], 'codex-x86_64-un', ''),
    ('agy', [BORDER, '> ', BORDER, 'Gemini 4 Neon (Low) · low'], 'agy', ''),
    ('agy', [BORDER, '> ', BORDER, 'Gemini 4 Neon (Low) · low'], 'mainthread',
     'node /home/demo/node_modules/@google/gemini-cli/dist/index.js'),
    ('dsh', [BORDER, '⌸ ❯ ', BORDER], 'mainthread', 'node /home/demo/node_modules/@deepseek-ai/dsh/lib/bin.js'),
    ('hermes', [BORDER, '❯ ', BORDER, ' ☤ fixture status'], 'python3', 'python3 -m hermes_cli.main'),
])
def test_unknown_paste_mode_requires_the_matching_editor_in_the_foreground_group(
    monkeypatch, editor, rows, command, argv,
):
    metadata = "/unused/tmux.sock\t100\t%1\t200\t1\t0\t2\t\tbash"
    view = frame(metadata, rows, '%1')
    assert view.empty and view.editor == editor
    paste = submission['confirm_wrapped_editor']
    processes = paste.__globals__
    editor_group = 200

    def ps(*args):
        if args[0] == '-ww':
            return argv + '\n'
        return (
            '200 100 200 200 pts/3 Ss bash\n'
            '201 200 200 200 pts/3 S bash\n'
            f'202 201 {editor_group} 200 pts/3 S+ {command}\n'
        )

    monkeypatch.setitem(processes, '_process_columns', ps)
    monkeypatch.setitem(processes, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setitem(processes, '_editor_has_terminal_input', lambda pid, tty: True)
    text = 'First line\nSecond line'
    assert not view.allows_paste(text)
    assert paste(view).allows_paste(text)
    assert not paste(view).allows_paste('First line\nSecond line\x1b[A')
    editor_group = 202  # A background descendant does not own the foreground.
    assert not paste(view).allows_paste(text)


@pytest.mark.parametrize(('platform', 'exe', 'argv0', 'confirmed'), [
    ('linux', '/home/demo/.local/bin/agy', 'renamed-process', True),
    ('linux', None, '/home/demo/.local/bin/agy', True),
    ('darwin', None, '/Users/demo/.local/bin/agy', True),
    ('linux', '/home/demo/.local/bin/not-agy', '/home/demo/.local/bin/not-agy', False),
])
def test_native_agy_under_a_shell_requires_its_executable_or_argv0(
    monkeypatch, platform, exe, argv0, confirmed,
):
    metadata = '/unused/tmux.sock\t100\t%1\t200\t1\t0\t2\t\tbash'
    view = frame(metadata, [BORDER, '> ', BORDER, 'auto'], '%1')
    confirm = submission['confirm_wrapped_editor']
    scope = confirm.__globals__
    monkeypatch.setitem(scope, 'sys', SimpleNamespace(platform=platform))
    monkeypatch.setitem(scope, '_process_columns', lambda *args:
                        '200 100 200 200 pts/3 Ss bash\n202 200 200 200 pts/3 S+ launcher\n')
    monkeypatch.setitem(scope, '_process_argv', lambda pid: [argv0, '--mode', 'auto', '/tmp/agy'])
    monkeypatch.setitem(scope, '_editor_has_terminal_input', lambda pid, tty: (pid, tty) == (202, 'pts/3'))

    def readlink(path):
        assert path == '/proc/202/exe'
        if exe is None:
            raise FileNotFoundError(path)
        return exe

    monkeypatch.setitem(scope, 'os', SimpleNamespace(readlink=readlink))
    assert view.editor == 'agy' and view.empty
    assert confirm(view).allows_paste('First line\nSecond line') is confirmed


@pytest.mark.parametrize(("highlight", "option", "expected"), [
    ("\x1b[1;7m", "/exit", "/exit"),
    ("\x1b[0;1m\x1b[38;2;0;0;46m\x1b[48;2;99;168;248m", "/exit", "/exit"),
    ("\x1b[0;1m\x1b[38;2;0;0;46m\x1b[48;2;99;168;248m", "/help", "/help"),
    ("\x1b[1m", "/exit", None),
    ("\x1b[48;2;99;168;248m\x1b[49m", "/exit", None),
])
def test_codex_completion_requires_active_row_highlight(highlight, option, expected):
    metadata = "/tmp/fixture.sock\t100\t%1\t200\t2\t0\t7\t1\tcodex"
    lines = [f"{highlight}› {option}  native command\x1b[0m", "",
             "\x1b[1m›\x1b[0m /exit", "", "  GPT-6.2-Sol high · Context 12% left"]
    view = frame(metadata, lines, "%1")
    assert view.draft == "/exit" and view.selected
    assert view.selected_command == expected
    assert submission["ready"](view, "/exit") is (expected == "/exit")
