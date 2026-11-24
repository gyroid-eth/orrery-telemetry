"""Native compact observations through the existing capture and relay projection."""
from types import SimpleNamespace

import pytest

from dashboard import server
from dashboard.mobile_projection import project_agent
from dashboard.terminal_state import reader_compaction


RULE = '─' * 22
CLAUDE_INPUT = f'{RULE}\n❯ \n{RULE}\n⏵⏵ auto mode on · esc to interrupt'
CODEX_INPUT = '› \ngpt-fixture high · 14% context left'
DSH_INPUT = f'╭{RULE}╮\n⌸ ❯       ⛶\n╰{RULE}╯\nctx 55% (70k/128k)'

# Use the native formats, including the dsh locale and the untimed Hermes path.
ACTIVE = [
    ('claude-code', '✶ Compacting conversation… (12s)\n' + CLAUDE_INPUT, '12s'),
    ('codex', '• Compacting context (2m 03s •\nesc to interrupt)\nMaking room to continue.\n' + CODEX_INPUT, '2m 03s'),
    ('dsh', '✶ Summarizing earlier turns… · reading context… · 7s · Esc cancels\n' + DSH_INPUT, '7s'),
    ('dsh', '· 正在压缩会话… · 读取上下文… · 1m3s · Esc 取消\n' + DSH_INPUT, '1m 3s'),
    ('hermes', '─ ☤ compacting… · 9s │ fixture-model\n❯', '9s'),
    ('hermes', '❯\n─ ⠋ compressing 8 messages (~1,200 tok)… │ fixture-model', ''),
]
FINISHED = {
    'claude-code': '⏺ Finished.\n' + CLAUDE_INPUT.replace(' · esc to interrupt', ''),
    'codex': '• Finished.\n' + CODEX_INPUT,
    'dsh': '⏺ Finished.\n' + DSH_INPUT,
    'hermes': '─ ready │ fixture-model\n❯',
}


@pytest.mark.parametrize('program,screen,elapsed', ACTIVE)
def test_runtime_and_mobile_use_one_capture_and_clear_on_finish(monkeypatch, tmp_path, program, screen, elapsed):
    name = 'FixtureAgent'
    session = dict(created=100, session_id='$1', activity=200, attached=False,
                   cmd='node', title='fixture')
    capture = SimpleNamespace(stdout=screen, returncode=0)
    calls, running = [], [True]

    def tmux(args, **kwargs):
        calls.append(args)
        assert args[0] == 'capture-pane'
        return capture

    monkeypatch.setattr(server, 'RUNTIME_DIR', str(tmp_path))
    monkeypatch.setattr(server, '_rt_cache', {})
    monkeypatch.setattr(server, '_NATIVE_CONTEXT_CACHE', {})
    monkeypatch.setattr(server, '_provider_captures', {})
    monkeypatch.setattr(server, 'tmux_state', lambda: {name: session})
    monkeypatch.setattr(server, 'agentmail_state', lambda: ({}, {}))
    monkeypatch.setattr(server, '_tmux_row_state', lambda *a: (program, 'agent', running[0], None))
    monkeypatch.setattr(server, '_tmux_run', tmux)
    for function in ('_codex_app_runtimes', '_deliverables_index', '_name_substitutions',
                     '_annotations', '_persistent_profiles', '_card_screen_launches',
                     '_history_binding_fields', '_agent_card_fields', '_claude_row_mail_fields'):
        monkeypatch.setattr(server, function, lambda *a, **k: {})
    monkeypatch.setattr(server, '_retired_names', lambda *a: set())
    monkeypatch.setattr(server, '_project_key', lambda: '')
    monkeypatch.setattr(server, '_batched_registrations', lambda: None)
    monkeypatch.setattr(server, '_resume_capability_for_row', lambda *a, **k: None)
    monkeypatch.setattr(server, '_provider_stall_view', lambda *a: None)

    row = server.build_agents()[0]
    expected = {'reader_phase': 'compacting', **({'reader_phase_elapsed': elapsed} if elapsed else {})}
    for result in (row, project_agent(row)):
        assert {k: v for k, v in result.items() if k.startswith('reader_phase')} == expected
    assert server.build_agents()[0] == row
    assert len(calls) == 1
    running[0] = False
    assert 'reader_phase' not in server.build_agents()[0]
    assert len(calls) == 1
    running[0] = True
    clock = server.time.monotonic()
    monkeypatch.setattr(server.time, 'monotonic', lambda: clock + server._RT_TTL + 1)
    capture.stdout = screen + '\n' + FINISHED[program]
    assert 'reader_phase' not in server.build_agents()[0]
    assert len(calls) == 2


@pytest.mark.parametrize('program,screen,elapsed', ACTIVE)
def test_previous_and_quoted_widgets_are_ignored(program, screen, elapsed):
    program = 'claude' if program == 'claude-code' else program
    assert reader_compaction(screen, (program,)) == (program, elapsed)
    assert reader_compaction(screen, ('ag',)) is None
    assert reader_compaction('\n'.join('> ' + line for line in screen.splitlines()), (program,)) is None
    assert reader_compaction(screen + '\nnew output' * 45, (program,)) is None


def test_project_mobile_only_for_running_agents_and_validate_elapsed():
    row = dict(name='FixtureAgent', running=True, reader_phase='compacting', reader_phase_elapsed='4m 12s')
    assert project_agent(row)['reader_phase_elapsed'] == '4m 12s'
    for value in ('4m 12s\nprivate prompt', None, {}, '2s' * 150):
        assert 'reader_phase_elapsed' not in project_agent({**row, 'reader_phase_elapsed': value})
    for overrides in ({'running': False}, {'retired': True}, {'reader_phase': 'unknown'}):
        projected = project_agent({**row, **overrides})
        assert 'reader_phase' not in projected and 'reader_phase_elapsed' not in projected
