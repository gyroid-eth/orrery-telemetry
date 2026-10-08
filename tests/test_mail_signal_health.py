"""Reproduce #226 with an isolated HOME and a private tmux server."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from dashboard import server

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def retired_signals(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    runtime = home / '.agentstack/runtime'
    signals = home / '.agentstack/mail/signals'
    agents = signals / 'projects/probe/agents'
    retired = agents / 'RetiredRecipient'
    retired.mkdir(parents=True)
    socket = 'mail-health-226-' + uuid.uuid4().hex
    tmux = shutil.which('tmux')
    if tmux is None:
        pytest.skip('tmux is required')
    commands = tmp_path / 'bin'
    commands.mkdir()
    wrapper = commands / 'tmux'
    wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(tmux) + ' -L ' + shlex.quote(socket) + ' "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('PATH', str(commands) + ':' + os.environ['PATH'])
    monkeypatch.setenv('AGENTSTACK_RUNTIME_DIR', str(runtime))
    monkeypatch.setenv('AGENTSTACK_SIGNALS_DIR', str(signals))
    monkeypatch.setenv('AGENTSTACK_PERSISTENT_DELIVER', str(tmp_path / 'no-headless-helper'))
    source = (ROOT / 'hooks/watch_agent_mail_signals.sh').read_text()
    # Execute the real delivery functions once; no service/lock or watch loop.
    source = source[:source.index('if command -v fswatch &>/dev/null; then')]
    source = source.replace('\nacquire_lock\n', '\n# fixture: no service lock\n')
    runner = tmp_path / 'watch-once.sh'
    runner.write_text(source + '\nprocess_existing_signals\nwait\n')
    def call(*args):
        return subprocess.run([str(wrapper), *args], capture_output=True, text=True, timeout=10, check=True)
    def write_signal(number, recipient=retired, old=False):
        recipient.mkdir(parents=True, exist_ok=True)
        path = recipient / f'{number}.signal'
        path.write_text(json.dumps({'message': {'id': number, 'from': 'FixtureSender', 'subject': 'fixture'}}))
        if old:
            stamp = time.time() - 2 * 86400
            os.utime(path, (stamp, stamp))
        return path
    try:
        call('new-session', '-d', '-s', 'Control', 'sleep 300')
        call('new-session', '-d', '-s', 'RetiredRecipient', "printf 'Claude\\n'; cat")
        write_signal(1)
        completed = subprocess.run(['/bin/bash', str(runner)], capture_output=True, text=True, timeout=45)
        assert completed.returncode == 0, completed.stderr
        state = json.loads((runtime / 'notify-state.json').read_text())
        assert state['RetiredRecipient:1']['last_result'] == 'success'
        assert not (retired / '1.signal').exists()
        call('kill-session', '-t', '=RetiredRecipient')
        for number in range(2, 53):
            write_signal(number, old=True)
        completed = subprocess.run(['/bin/bash', str(runner)], capture_output=True, text=True, timeout=60)
        assert completed.returncode == 0, completed.stderr
        state = json.loads((runtime / 'notify-state.json').read_text())
        assert all(state[f'RetiredRecipient:{n}']['last_result'] == 'session_not_found' for n in range(2, 53))
        assert len(list(retired.glob('*.signal'))) == 51
        monkeypatch.setattr(server, '_signal_agent_dirs', lambda: [str(agents)])
        monkeypatch.setattr(server, 'NOTIFY_STATE_FILE', str(runtime / 'notify-state.json'))
        monkeypatch.setattr(server, 'RUNTIME_DIR', str(runtime))
        monkeypatch.setattr(server, '_launchctl_job_running', lambda label: False)
        monkeypatch.setattr(server, '_systemd_user_unit_running', lambda label: False)
        monkeypatch.setattr(server, '_pidfile_process_running', lambda *a: (True, 1234))
        monkeypatch.setattr(server, '_MAIL_HEALTH_CACHE', {'ts': 0., 'data': None})
        yield home, runtime, signals, agents, write_signal
    finally:
        subprocess.run([str(wrapper), 'kill-server'], capture_output=True, timeout=10)


def test_retired_signal_backlog_does_not_make_healthy_delivery_red(retired_signals):
    health = server.mail_watcher_health()
    assert health['watcher_running'] is True
    assert health['status'] == 'green'
    assert health['signal_count'] == 51
    assert health['actionable_signal_count'] == 0
    assert health['orphan_signal_count'] == 51


def test_doctor_warns_about_orphans_without_failing(retired_signals):
    health = server.mail_watcher_health()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(health).encode())
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    source = (ROOT / 'scripts/doctor.sh').read_text()
    start = source.index('report_mail_watcher_health() {')
    end = source.index('\nreport_dashboard_service()', start)
    try:
        command = 'set -euo pipefail\nstatus=0\nINSTALL_DIR="$HOME/.agentstack"\n' + source[start:end] + '\nreport_mail_watcher_health "$1" "$2"\nexit "$status"\n'
        result = subprocess.run(['/bin/bash', '-c', command, 'doctor-fixture', shutil.which('python3'), str(http.server_port)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert 'warn:' in result.stderr
        assert '51' in result.stderr
        assert '--prune' in result.stderr
        assert 'mail_signals.py' in result.stderr
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=10)


@pytest.mark.parametrize('recipient,old,headless', [('Control', True, False), ('RecentRecipient', False, False), ('HeadlessRecipient', True, True)])
def test_real_backlogs_still_turn_red(tmp_path, monkeypatch, recipient, old, headless):
    from dashboard import mail_signals
    agents = tmp_path / 'agents'
    folder = agents / recipient
    folder.mkdir(parents=True)
    runtime = tmp_path / 'runtime'
    if headless:
        (runtime / 'persistent').mkdir(parents=True)
        (runtime / 'persistent' / f'{recipient}.json').write_text('{}')
    for number in range(51):
        path = folder / f'{number}.signal'
        path.write_text('{}')
        if old:
            stamp = time.time() - 2 * 86400
            os.utime(path, (stamp, stamp))
    monkeypatch.setattr(mail_signals, 'session_names', lambda: {'Control'})
    monkeypatch.setattr(server, '_signal_agent_dirs', lambda: [str(agents)])
    monkeypatch.setattr(server, 'RUNTIME_DIR', str(runtime))
    monkeypatch.setattr(server, 'NOTIFY_STATE_FILE', str(tmp_path / 'absent-state'))
    monkeypatch.setattr(server, '_launchctl_job_running', lambda label: False)
    monkeypatch.setattr(server, '_systemd_user_unit_running', lambda label: False)
    monkeypatch.setattr(server, '_pidfile_process_running', lambda *a: (True, 1234))
    monkeypatch.setattr(server, '_MAIL_HEALTH_CACHE', {'ts': 0., 'data': None})
    health = server.mail_watcher_health()
    assert health['status'] == 'red'
    assert health['actionable_signal_count'] == 51


def test_unknown_sessions_retain_all_backlog(tmp_path, monkeypatch):
    from dashboard import mail_signals
    folder = tmp_path / 'agents/Absent'
    folder.mkdir(parents=True)
    path = folder / '1.signal'
    path.write_text('{}')
    stamp = time.time() - 2 * 86400
    os.utime(path, (stamp, stamp))
    monkeypatch.setattr(mail_signals, 'session_names', lambda: None)
    counts = mail_signals.counts([str(folder.parent)], str(tmp_path / 'runtime'), time.time())
    assert counts['signal_sessions_known'] is False
    assert counts['actionable_signal_count'] == 1
    with pytest.raises(ValueError, match='cannot confirm'):
        mail_signals.prune(tmp_path / 'signals', tmp_path / 'runtime', apply=True)
    assert path.exists()


@pytest.mark.parametrize('failure', ['timeout', 'missing-tool', 'permission'])
def test_session_measurement_failures_are_not_absence(monkeypatch, failure):
    from dashboard import mail_signals
    def fail(*args, **kwargs):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('tmux', 3)
        if failure == 'missing-tool':
            raise FileNotFoundError('tmux')
        return subprocess.CompletedProcess(args[0], 1, '', 'permission denied')
    monkeypatch.setattr(mail_signals.subprocess, 'run', fail)
    assert mail_signals.session_names() is None


def test_prune_is_opt_in_and_rechecks_sessions_and_signal_identity(tmp_path, monkeypatch):
    from dashboard import mail_signals
    signals = tmp_path / 'signals'
    agents = signals / 'projects/probe/agents'
    folder = agents / 'Absent'
    folder.mkdir(parents=True)
    old = time.time() - 2 * 86400
    def make(path):
        path.write_text('{}')
        os.utime(path, (old, old))
        return path
    target = make(folder / '1.signal')
    legacy = make(agents / 'Legacy.signal')
    live = agents / 'Alive'
    live.mkdir()
    make(live / '2.signal')
    recent = folder / '3.signal'
    recent.write_text('{}')
    outside = tmp_path / 'untouched.signal'
    make(outside)
    (folder / 'linked.signal').symlink_to(outside)
    state = tmp_path / 'runtime/notify-state.json'
    state.parent.mkdir()
    state.write_text('do not change')
    monkeypatch.setattr(mail_signals, 'session_names', lambda: {'Alive'})
    assert mail_signals.prune(signals, state.parent) == 2
    assert target.exists() and legacy.exists()
    assert mail_signals.prune(signals, state.parent, apply=True) == 2
    assert not target.exists() and not legacy.exists()
    assert (live / '2.signal').exists() and recent.exists()
    assert outside.read_text() == '{}'
    assert state.read_text() == 'do not change'
    make(target)
    answers = iter([{'Alive'}, {'Alive', 'Absent'}])
    monkeypatch.setattr(mail_signals, 'session_names', lambda: next(answers))
    assert mail_signals.prune(signals, state.parent, apply=True) == 0
    assert target.exists()
    # Refresh between inventory and the unlink recheck: preserve new content.
    def refresh():
        target.write_text('new signal')
        return {'Alive'}
    monkeypatch.setattr(mail_signals, 'session_names', refresh)
    assert mail_signals.prune(signals, state.parent, apply=True) == 0
    assert target.read_text() == 'new signal'


def test_prune_cannot_follow_symlinked_signal_ancestors(tmp_path, monkeypatch):
    from dashboard import mail_signals
    outside = tmp_path / 'outside'
    folder = outside / 'agents/Absent'
    folder.mkdir(parents=True)
    target = folder / '1.signal'
    target.write_text('{}')
    old = time.time() - 2 * 86400
    os.utime(target, (old, old))
    projects = tmp_path / 'signals/projects'
    projects.mkdir(parents=True)
    (projects / 'probe').symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(mail_signals, 'session_names', lambda: set())
    assert mail_signals.prune(projects.parent, tmp_path / 'runtime', apply=True) == 0
    assert target.exists()
    (tmp_path / 'alias').symlink_to(projects.parent, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        mail_signals.prune(tmp_path / 'alias', tmp_path / 'runtime', apply=True)


@pytest.mark.parametrize('diagnostic', ['no server running on /tmp/fixture-socket', 'no sessions', 'error connecting to /tmp/fixture-socket (No such file or directory)'])
def test_known_absent_server_means_no_sessions(monkeypatch, diagnostic):
    from dashboard import mail_signals
    monkeypatch.setattr(mail_signals.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a[0], 1, '', diagnostic))
    assert mail_signals.session_names() == set()


def test_legacy_signals_and_exact_session_names(tmp_path, monkeypatch):
    from dashboard import mail_signals
    agents = tmp_path / 'agents'
    agents.mkdir()
    path = agents / 'Recipient.signal'
    path.write_text('{}')
    old = time.time() - 2 * 86400
    os.utime(path, (old, old))
    monkeypatch.setattr(mail_signals, 'session_names', lambda: {'RecipientLonger'})
    assert mail_signals.counts([str(agents)], str(tmp_path / 'runtime'), time.time())['orphan_signal_count'] == 1
    monkeypatch.setattr(mail_signals, 'session_names', lambda: {'Recipient'})
    assert mail_signals.counts([str(agents)], str(tmp_path / 'runtime'), time.time())['actionable_signal_count'] == 1


def test_refresh_during_session_recheck_preserves_signal(tmp_path, monkeypatch):
    from dashboard import mail_signals
    signals = tmp_path / 'signals'
    folder = signals / 'projects/probe/agents/Absent'
    folder.mkdir(parents=True)
    path = folder / '1.signal'
    path.write_text('{}')
    old = time.time() - 2 * 86400
    os.utime(path, (old, old))
    calls = 0
    def sessions():
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text('refreshed during tmux probe')
        return set()
    monkeypatch.setattr(mail_signals, 'session_names', sessions)
    assert mail_signals.prune(signals, tmp_path / 'runtime', apply=True) == 0
    assert path.read_text() == 'refreshed during tmux probe'


def test_cleanup_cli_uses_installed_environment_and_defaults_to_preview(tmp_path):
    import sys
    home = tmp_path / 'home'
    helper = home / '.agentstack/dashboard/mail_signals.py'
    helper.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / 'dashboard/mail_signals.py', helper)
    signals = home / 'configured-signals'
    folder = signals / 'projects/probe/agents/Absent'
    folder.mkdir(parents=True)
    path = folder / '1.signal'
    path.write_text('{}')
    old = time.time() - 2 * 86400
    os.utime(path, (old, old))
    commands = tmp_path / 'commands'
    commands.mkdir()
    tmux = commands / 'tmux'
    tmux.write_text('#!/bin/sh\necho "no server running on fixture" >&2\nexit 1\n')
    tmux.chmod(0o755)
    env = {'HOME': str(home), 'PATH': str(commands) + ':' + os.environ['PATH'],
           'AGENTSTACK_SIGNALS_DIR': '~/configured-signals', 'AGENTSTACK_RUNTIME_DIR': '~/runtime'}
    preview = subprocess.run([sys.executable, str(helper)], env=env, capture_output=True, text=True, timeout=10)
    assert preview.returncode == 0, preview.stderr
    assert '1 signals eligible' in preview.stdout
    assert path.exists()
    applied = subprocess.run([sys.executable, str(helper), '--prune'], env=env, capture_output=True, text=True, timeout=10)
    assert applied.returncode == 0, applied.stderr
    assert '1 signals removed' in applied.stdout
    assert not path.exists()
