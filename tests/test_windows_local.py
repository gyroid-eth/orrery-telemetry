"""Actual Windows process startup, reuse, conflict and shutdown boundaries."""
import importlib.util
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('windows_local', ROOT / 'scripts/windows_local.py')
local = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(local)
pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='native Windows process semantics')


def ports():
    with socket.socket() as first, socket.socket() as second:
        first.bind(('127.0.0.1', 0))
        second.bind(('127.0.0.1', 0))
        return first.getsockname()[1], second.getsockname()[1]


def command(project, state, mail_port, dashboard_port):
    return ['powershell.exe', '-NoProfile', '-File', str(ROOT / 'scripts/start-windows.ps1'),
            '-Project', str(project), '-StateDirectory', str(state),
            '-PythonCommand', sys.executable,
            '-MailPort', str(mail_port), '-DashboardPort', str(dashboard_port), '-NoBrowser']


def test_fresh_start_reuse_conflict_and_owned_shutdown(tmp_path):
    project = tmp_path / 'project with spaces'
    project.mkdir()
    state = tmp_path / 'persistent state'
    mail_port, dashboard_port = ports()
    args = command(project, state, mail_port, dashboard_port)
    dry = subprocess.run([*args, '-DryRun'], capture_output=True, text=True, timeout=20)
    assert dry.returncode == 0, dry.stderr
    assert not state.exists()
    assert json.loads(dry.stdout)['mail'] == 'start'
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    output = queue.Queue()
    def read():
        for line in process.stdout:
            output.put(line)
    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    lines = []
    try:
        while True:
            line = output.get(timeout=60)
            lines.append(line)
            if line.startswith('Ready:'):
                break
        assert (state / 'storage.sqlite3').is_file()
        mail_pid = local.listener(mail_port).pid
        dashboard_pid = local.listener(dashboard_port).pid
        reuse = subprocess.run(args, capture_output=True, text=True, timeout=20)
        assert reuse.returncode == 0, reuse.stderr
        assert json.loads(reuse.stdout.splitlines()[0])['mail'] == 'reuse'
        assert local.listener(mail_port).pid == mail_pid
        assert local.listener(dashboard_port).pid == dashboard_pid
        conflict_state = tmp_path / 'wrong state'
        conflict = subprocess.run(command(project, conflict_state, mail_port, dashboard_port),
                                  capture_output=True, text=True, timeout=20)
        assert conflict.returncode == 1
        assert 'different database' in conflict.stderr
        assert not conflict_state.exists()
        assert local.listener(mail_port).pid == mail_pid
        other_project = tmp_path / 'other project'
        other_project.mkdir()
        conflict = subprocess.run(command(other_project, state, mail_port, dashboard_port),
                                  capture_output=True, text=True, timeout=20)
        assert conflict.returncode == 1
        assert 'configuration differs' in conflict.stderr
    finally:
        if process.poll() is None:
            process.stdin.write('\n')
            process.stdin.flush()
        process.wait(timeout=35)
        process.stdin.close()
        thread.join(timeout=5)
        process.stdout.close()
    assert process.returncode == 0, ''.join(lines)
    assert local.listener(mail_port) is None
    assert local.listener(dashboard_port) is None
    assert (state / 'storage.sqlite3').exists()


def test_foreign_port_is_not_replaced(tmp_path):
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        mail_port = occupied.getsockname()[1]
        _, dashboard_port = ports()
        state = tmp_path / 'untouched'
        result = subprocess.run(command(tmp_path, state, mail_port, dashboard_port),
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 1
        assert not state.exists()
        assert local.listener(mail_port).pid == os.getpid()
