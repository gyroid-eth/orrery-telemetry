"""Ordinary legacy callers use the real isolated default tmux endpoint."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def native_default(tmp_path, monkeypatch):
    binary = shutil.which('tmux')
    if not binary:
        pytest.skip('tmux unavailable')
    with tempfile.TemporaryDirectory(prefix='r263default-', dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as directory:
        base = Path(directory).resolve()
        home = tmp_path / 'home'
        home.mkdir()
        stack = tmp_path / 'stack'
        stack.mkdir(mode=0o700)
        monkeypatch.setenv('HOME', str(home))
        monkeypatch.setenv('AGENTSTACK_HOME', str(stack))
        monkeypatch.setenv('AGENTSTACK_RUNTIME_DIR', str(stack / 'runtime'))
        monkeypatch.setenv('AGENTSTACK_LABEL_PREFIX', 'org.agentstack.test.native-legacy')
        monkeypatch.setenv('AGENTSTACK_MCP_URL', 'http://127.0.0.1:9/mcp')
        monkeypatch.setenv('TMUX_TMPDIR', str(base))
        monkeypatch.delenv('TMUX', raising=False)
        monkeypatch.delenv('TMUX_PANE', raising=False)
        path = base / ('tmux-' + str(os.getuid())) / 'default'
        try:
            yield binary, path, home, stack
        finally:
            subprocess.run([binary, '-S', str(path), 'kill-server'], capture_output=True, timeout=5)


def test_actual_legacy_spawn_keeps_provider_argv_on_native_default(native_default, tmp_path, monkeypatch):
    binary, path, home, stack = native_default
    fake = tmp_path / 'fixture-bin'
    fake.mkdir()
    provider = fake / 'claude'
    argv_file = tmp_path / 'provider-argv.json'
    import sys
    provider.write_text('#!' + sys.executable + '\nimport json,sys,time\n'
                        'from pathlib import Path\n'
                        f'Path({str(argv_file)!r}).write_text(json.dumps(sys.argv[1:]))\n'
                        'print("\\n❯ ",flush=True)\ntime.sleep(90)\n')
    provider.chmod(0o700)
    monkeypatch.setenv('PATH', str(fake) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('AGENTSTACK_HOOKS_DIR', str(ROOT / 'hooks'))
    monkeypatch.setenv('AGENTSTACK_REGISTER_LIB', str(ROOT / 'bin/lib/agentstack-register.sh'))
    monkeypatch.setenv('AGENTSTACK_MCP_PROXY', str(tmp_path / 'missing-proxy'))
    monkeypatch.setenv('AGENTSTACK_TERMINAL', 'none')
    monkeypatch.setenv('AGENTSTACK_RUNTIME_MODE', 'legacy')
    monkeypatch.setenv('PARENT_AGENT', 'FixtureParent')
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(home / '.claude'))
    token = tmp_path / 'fixture-token'
    token.write_text('fixture-owner-token')
    token.chmod(0o600)
    binding = token.with_name(token.name + '.binding.json')
    binding.write_text(json.dumps({'agent_id': 73, 'agent_name': 'FixtureLegacy', 'project_key': '/fixture/project', 'program': 'claude-code'}))
    binding.chmod(0o600)
    monkeypatch.setenv('AGENTSTACK_PROJECT_KEY', '/fixture/project')
    monkeypatch.setenv('PROJECT_KEY', '/fixture/project')
    task = tmp_path / 'task.md'
    task.write_text('Read this synthetic task without contacting any service.')
    work = tmp_path / 'work'
    work.mkdir()
    result = subprocess.run(['/bin/bash', str(ROOT / 'hooks/spawn_child.sh'), '--pre-registered', 'FixtureLegacy', '--child-token-file', str(token), '--embed-task', '--task-file', str(task), str(work)],
                            env=os.environ.copy(), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    sessions = subprocess.run([binary, '-S', str(path), 'list-sessions', '-F', '#{session_name}'], capture_output=True, text=True, check=True)
    assert sessions.stdout.splitlines() == ['FixtureLegacy']
    argv = json.loads(argv_file.read_text())
    assert argv[argv.index('--model') + 1] == 'claude-opus-5-5'
    assert argv[-1].endswith(task.read_text())
    assert '--chrome' not in argv


def test_actual_legacy_resume_keeps_native_default_replacement(native_default, tmp_path, monkeypatch):
    from dashboard import server
    binary, path, _, _ = native_default
    monkeypatch.setattr(server, 'HOOKS_DIR', str(ROOT / 'hooks'))
    monkeypatch.setattr(server, '_open_terminal_tmux', lambda *a, **kw: {'ok': True, 'adapter': 'fixture'})
    old = subprocess.run([binary, 'new-session', '-d', '-P', '-F', '#{session_id}', '-s', 'FixtureResume', '/bin/sleep', '90'], capture_output=True, text=True, check=True).stdout.strip()
    marker = tmp_path / 'started'
    command = 'printf started > ' + str(marker) + '; /bin/sleep 90'
    reply = server._launch_claude_resume_tmux(['tmux', 'new-session', '-A', '-s', 'FixtureResume', '-c', str(tmp_path), '/bin/bash', '-c', command], title='FixtureResume', open_terminal=False, replace_husk=True)
    assert reply['ok'], reply
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert marker.read_text() == 'started'
    sessions = subprocess.run([binary, '-S', str(path), 'list-sessions', '-F', '#{session_id}:#{session_name}'], capture_output=True, text=True, check=True).stdout.splitlines()
    assert len(sessions) == 1 and sessions[0].endswith(':FixtureResume')
    assert not sessions[0].startswith(old + ':')
