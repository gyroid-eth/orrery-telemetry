"""Experimental foreground supervisor for a checkout's Windows Mail/dashboard."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

import psutil

ROOT = Path(__file__).resolve().parents[1]


def request(url: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        'Content-Type': 'application/json', 'Accept': 'application/json'})
    # Loopback requests must not go through an ambient HTTP proxy.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3) as response:
        return json.load(response)


def mail_health(url: str, database: Path) -> None:
    result = request(url, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                          'params': {'name': 'health_check', 'arguments': {}}})['result']
    if result.get('isError'):
        raise RuntimeError('Mail health_check failed')
    health = result.get('structuredContent')
    if health is None:
        health = json.loads(next(part['text'] for part in result['content'] if part['type'] == 'text'))
    prefix = 'sqlite+aiosqlite:///'
    url_value = health.get('database_url', '')
    if health.get('status') != 'ok' or not url_value.startswith(prefix):
        raise RuntimeError('Mail did not identify a healthy SQLite database')
    if Path(url_value[len(prefix):]).resolve() != database:
        raise RuntimeError('Mail is using a different database; pass its actual -StateDirectory')


def listener(port: int) -> psutil.Process | None:
    matches = [c for c in psutil.net_connections(kind='tcp')
               if c.status == psutil.CONN_LISTEN and c.laddr.port == port]
    if not matches:
        return None
    if any(c.laddr.ip != '127.0.0.1' or c.pid is None for c in matches):
        raise RuntimeError(f'Port {port} has an unexpected listener; leave it running and resolve the conflict')
    pids = {c.pid for c in matches}
    if len(pids) != 1:
        raise RuntimeError(f'Port {port} has multiple listeners')
    return psutil.Process(pids.pop())


def dashboard_health(url: str) -> None:
    version = request(url + 'api/version')
    if version.get('name') != 'claude-agent-stack' or version.get('api') != 1:
        raise RuntimeError('Dashboard endpoint is not the expected ORRERY API')
    request(url + 'api/agents')
    request(url + 'api/graph?all=1')


def verify_dashboard(process: psutil.Process, database: Path, project: Path, mail_url: str) -> None:
    if process.username() != psutil.Process().username():
        raise RuntimeError('Dashboard belongs to a different user')
    env = process.environ()
    if (Path(env.get('AGENTSTACK_MAIL_DB', '')).resolve() != database
            or Path(env.get('AGENTSTACK_PROJECT_KEY', '')).resolve() != project
            or env.get('AGENTSTACK_MCP_URL') != mail_url
            or 'dashboard.server' not in process.cmdline()):
        raise RuntimeError('Existing Dashboard configuration differs; leave it running and resolve the conflict')


def wait_ready(process: subprocess.Popen, check) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('A started process exited; inspect the state directory logs')
        try:
            check()
            return
        except (OSError, ValueError, KeyError, RuntimeError):
            time.sleep(0.25)
    raise RuntimeError('Startup timed out; inspect the state directory logs')


def stop_owned(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.send_signal(signal.CTRL_BREAK_EVENT)
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        print('Graceful stop unavailable; terminating only this invocation\'s child process.', flush=True)
        process.terminate()
        process.wait(timeout=10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--state-directory', type=Path, required=True)
    parser.add_argument('--mail-port', type=int, default=18765)
    parser.add_argument('--dashboard-port', type=int, default=8770)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)
    if sys.platform != 'win32':
        raise RuntimeError('This experimental helper requires native Windows')
    if not shutil.which('git'):
        raise RuntimeError('git must be on PATH')
    if not shutil.which('tmux'):
        print('tmux is missing: runtime session discovery is unavailable.', file=sys.stderr)
    if not (1 <= args.mail_port <= 65535 and 1 <= args.dashboard_port <= 65535
            and args.mail_port != args.dashboard_port):
        raise RuntimeError('Choose two distinct valid ports')
    project = args.project.resolve(strict=True)
    state = args.state_directory.resolve()
    if not project.is_dir() or state == project or project in state.parents:
        raise RuntimeError('State directory must be outside the project directory')
    database = state / 'storage.sqlite3'
    mail_url = f'http://127.0.0.1:{args.mail_port}/mcp'
    dashboard_url = f'http://127.0.0.1:{args.dashboard_port}/'
    mail = listener(args.mail_port)
    dashboard = listener(args.dashboard_port)
    if mail:
        if mail.username() != psutil.Process().username() or 'agentstack_mail.cli' not in mail.cmdline():
            raise RuntimeError('Existing Mail is not this user\'s native Mail entry point')
        mail_health(mail_url, database)
    if dashboard:
        verify_dashboard(dashboard, database, project, mail_url)
        dashboard_health(dashboard_url)
    print(json.dumps({'mode': 'dry-run' if args.dry_run else 'experimental-local',
                      'mail': 'reuse' if mail else 'start',
                      'dashboard': 'reuse' if dashboard else 'start',
                      'database': str(database), 'project': str(project),
                      'dashboard_url': dashboard_url}), flush=True)
    if args.dry_run:
        return 0
    # Isolate generated service configuration from personal env files and settings.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('AGENTSTACK_', 'MCP_AGENT_MAIL_', 'HTTP_BEARER_'))}
    env.update({
        'PYTHONUTF8': '1', 'PYTHONPATH': str(ROOT) + os.pathsep + str(ROOT / 'packages/agentstack_mail/src'),
        'AGENTSTACK_MAIL_ENV_FILE': str(state / '.unused-env'),
        'AGENTSTACK_MAIL_ENV': str(state / '.unused-env'),
        'AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE': 'passthrough',
        'AGENTSTACK_MAIL_DATABASE_URL': 'sqlite+aiosqlite:///' + database.as_posix(),
        'AGENTSTACK_MAIL_STORAGE_ROOT': str(state / 'archive'),
        'AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR': str(state / 'signals'),
        'AGENTSTACK_MAIL_HTTP_PORT': str(args.mail_port),
        'AGENTSTACK_MAIL_DB': str(database), 'AGENTSTACK_MAIL_HOME': str(state),
        'AGENTSTACK_SIGNALS_DIR': str(state / 'signals'),
        'AGENTSTACK_PROJECT_KEY': str(project), 'AGENTSTACK_MCP_URL': mail_url,
        'AGENTSTACK_PORT': str(args.dashboard_port), 'AGENTSTACK_BIND_HOST': '127.0.0.1',
        'AGENTSTACK_RUNTIME_DIR': str(state / 'runtime'),
        'AGENTSTACK_DASHBOARD_LOG': str(state / 'dashboard.log'),
        'AGENTSTACK_DASHBOARD_STATE': str(state / 'runtime/dashboard-service-state.json'),
    })
    if (state / '.unused-env').exists():
        raise RuntimeError('Reserved .unused-env path exists; refusing to load an unexpected env file')
    owned = []
    logs = []

    def start(module: str, extra: list[str]) -> subprocess.Popen:
        state.mkdir(parents=True, exist_ok=True)
        log = (state / (module + '.log')).open('ab')
        logs.append(log)
        process = subprocess.Popen([sys.executable, '-X', 'utf8', '-m', module, *extra],
                                   cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        owned.append(process)
        return process

    try:
        if not mail:
            child = start('agentstack_mail.cli', ['--host', '127.0.0.1', '--port', str(args.mail_port), '--path', '/mcp'])
            wait_ready(child, lambda: mail_health(mail_url, database))
        if not dashboard:
            child = start('dashboard.server', [])
            wait_ready(child, lambda: dashboard_health(dashboard_url))
        print('Ready: ' + dashboard_url, flush=True)
        if not args.no_browser and not webbrowser.open(dashboard_url):
            print('Browser could not be opened automatically; open the Ready URL.', flush=True)
        if owned:
            stopped = threading.Event()
            def read_stop() -> None:
                try:
                    input('Keep this terminal open. Press Enter to stop processes started here.\n')
                except EOFError:
                    pass
                finally:
                    stopped.set()
            threading.Thread(target=read_stop, daemon=True).start()
            while not stopped.wait(0.5):
                if any(process.poll() is not None for process in owned):
                    raise RuntimeError('A managed process exited; inspect the state directory logs')
        return 0
    except (KeyboardInterrupt, EOFError):
        return 0
    finally:
        for process in reversed(owned):
            stop_owned(process)
        for log in logs:
            log.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'ORRERY startup stopped: {error}', file=sys.stderr)
        raise SystemExit(1)
