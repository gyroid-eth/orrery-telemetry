"""One supervised Mac observation window; never signals a process or reads keys.

Startup network calls occur normally and can expose live data. Observe only
owned processes, with exact destinations; snapshots can miss short connections.
"""
import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit


def read(path):
    return json.loads(Path(path).read_text())


def stamp():
    return datetime.now(timezone.utc).isoformat()


def write(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as out:
        out.write(json.dumps(value, ensure_ascii=False) + '\n')


def destinations(config, resolve=socket.getaddrinfo):
    backend = read(config['backend_file'])['backends'][config['backend_id']]
    strata = urlsplit(backend['base_url'])
    mail = urlsplit(config['connection']['mcp_url'])
    if (config['backend_id'] != 'strata' or strata.scheme != 'https' or not strata.hostname
            or strata.port not in (None, 443) or strata.username or strata.password
            or mail.scheme not in ('http', 'https') or mail.hostname != '127.0.0.1'
            or mail.username or mail.password):
        raise ValueError('Exact Strata HTTPS and existing loopback Mail endpoints required')
    port = mail.port or (443 if mail.scheme == 'https' else 80)
    ips = sorted({str(ipaddress.ip_address(row[4][0]))
                  for row in resolve(strata.hostname, 443, type=socket.SOCK_STREAM)})
    if not ips:
        raise ValueError('Strata address is unresolved')
    return {(ip, 443) for ip in ips} | {('127.0.0.1', port)}, {
        'strata_host': strata.hostname, 'strata_ips': ips, 'strata_port': 443,
        'mail_ip': '127.0.0.1', 'mail_port': port}


def processes(text):
    rows = {}
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and all(value.isdecimal() for value in parts[:3]):
            pid, ppid, pgid = map(int, parts[:3])
            rows[pid] = {'pid': pid, 'ppid': ppid, 'pgid': pgid,
                         'command': Path(parts[3]).name}
    return rows


def descendants(table, roots, pgid=None):
    selected = {pid for pid, row in table.items() if pid in roots or row['pgid'] == pgid}
    while True:
        expanded = selected | {pid for pid, row in table.items() if row['ppid'] in selected}
        if expanded == selected:
            return selected
        selected = expanded


def sockets(text):
    result, pid, command, current = [], None, None, None
    for line in text.splitlines():
        if not line:
            continue
        field, value = line[0], line[1:]
        if field == 'p':
            pid, current = int(value), None
        elif field == 'c':
            command = value
        elif field == 'f':
            current = {'pid': pid, 'command': command}
            result.append(current)
        elif current is not None:
            if field == 'P':
                current['protocol'] = value
            elif field == 'n':
                current['connection'] = value
            elif field == 'T' and value.startswith('ST='):
                current['state'] = value[3:]
    return result


def allowed(row, permitted):
    if row.get('protocol') != 'TCP' or '->' not in row.get('connection', ''):
        return False
    remote = row['connection'].split('->', 1)[1]
    try:
        host, port = remote.rsplit(':', 1)
        return (str(ipaddress.ip_address(host.strip('[]'))), int(port)) in permitted
    except ValueError:
        return False


class Mac:
    def __init__(self, config):
        if sys.platform != 'darwin':
            raise ValueError('This observation adapter supports macOS only')
        self.config = config
        self.env = {'HOME': config['home'], 'PATH': '/usr/bin:/bin:/usr/sbin:/opt/homebrew/bin:/usr/local/bin'}
        self.lsof = shutil.which('lsof', path=self.env['PATH'])
        self.tmux = shutil.which('tmux', path=self.env['PATH'])
        if not self.lsof or not self.tmux:
            raise ValueError('lsof and tmux are required before starting')

    def run(self, argv):
        return subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=5)

    def table(self):
        result = self.run(['/bin/ps', '-axo', 'pid=,ppid=,pgid=,comm='])
        if result.returncode or result.stderr:
            raise ValueError('Process observation unavailable')
        return processes(result.stdout)

    def pane_pid(self, pane):
        result = self.run([self.tmux, 'display-message', '-p', '-t', pane, '#{pane_pid}'])
        return int(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip().isdecimal() else None

    def connections(self, pids):
        if not pids:
            return []
        result = self.run([self.lsof, '-nP', '-a', '-p', ','.join(map(str, sorted(pids))), '-i', '-FpcfPnT'])
        # lsof returns 1 for no matching sockets. Diagnostics are not a clean sample.
        if result.returncode not in (0, 1) or result.stderr:
            raise ValueError('Socket observation unavailable')
        return sockets(result.stdout)

    def start(self, state):
        return subprocess.Popen([sys.executable, str(Path(__file__).with_name('launch.py')),
                                 'start', '--state', str(state)], env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def observe(state, *, system=None, resolve=socket.getaddrinfo, maximum=900, interval=1,
            clock=time.monotonic, sleep=time.sleep):
    state = Path(state).resolve()
    config, attempt = read(state / 'run-config.json'), read(state / 'attempt.json')
    if 'DEEPSEEK_API_KEY' in os.environ or attempt['phase'] != 'run':
        raise ValueError('Observer must start keyless before the managed run')
    if any((state / name).exists() for name in ('run-started.json', 'runtime-process.json', 'runtime-exit.json')):
        raise ValueError('Observe a fresh, preflighted attempt before start')
    permitted, mapping = destinations(config, resolve)
    system = system or Mac(config)
    log = state / 'network-observation.jsonl'
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    started, roots, pgid, seen_runtime, completed = clock(), set(), None, False, False
    errors, forbidden, samples, child = [], [], 0, None
    with os.fdopen(fd, 'w') as out:
        def save(value):
            out.write(json.dumps({'at': stamp(), **value}, ensure_ascii=False) + '\n')
            out.flush()
        save({'phase': 'observation-start', 'attempt_id': attempt['attempt_id'], **mapping})
        try:
            system.table()  # Confirm process access before launch.
            child = system.start(state)
            roots.add(child.pid)
            while clock() - started < maximum:
                tick = clock()
                pane_file, runtime_file = state / 'owned-pane.json', state / 'runtime-process.json'
                if pane_file.exists():
                    pane = read(pane_file)
                    if pane['attempt_id'] != attempt['attempt_id']:
                        raise ValueError('Pane attempt mismatch')
                    pid = system.pane_pid(pane['pane_id'])
                    if pid:
                        roots.add(pid)
                if runtime_file.exists():
                    runtime = read(runtime_file)
                    if runtime['attempt_id'] != attempt['attempt_id']:
                        raise ValueError('Runtime attempt mismatch')
                    roots.update([runtime['pid'], runtime['supervisor_pid']])
                    pgid, seen_runtime = runtime['pgid'], True
                table = system.table()
                owned = descendants(table, roots, pgid)
                roots.update(owned)
                rows = system.connections(owned)
                for row in rows:
                    if row['pid'] not in owned:
                        raise ValueError('Socket PID outside owned set')
                    row['allowed'] = allowed(row, permitted)
                    if not row['allowed'] and row not in forbidden:
                        forbidden.append(row)
                        print('OS観測: 許可外の接続先あり。親がowned runtimeを停止してください。', flush=True)
                save({'phase': 'snapshot', 'processes': [table[pid] for pid in sorted(owned)], 'sockets': rows})
                samples += 1
                exit_file = state / 'runtime-exit.json'
                if exit_file.exists():
                    exit_receipt = read(exit_file)
                    if exit_receipt.get('group_absent') is True and not owned and child.poll() is not None:
                        completed = seen_runtime
                        break
                if child.poll() not in (None, 0) and not owned:
                    raise ValueError('Managed start failed')
                sleep(max(0, interval - (clock() - tick)))
            if not completed:
                errors.append('window-incomplete')
        except (Exception, KeyboardInterrupt):
            # No arbitrary exception text/env/argv or process stderr is persisted.
            errors.append('observation-unavailable-or-interrupted')
        result = {'attempt_id': attempt['attempt_id'], 'completed': completed, 'samples': samples,
                  'forbidden_observed': bool(forbidden), 'forbidden': forbidden, 'errors': errors,
                  'verdict': 'forbidden-observed' if forbidden else 'unknown' if errors else 'none-observed',
                  'limitation': 'One-second snapshots can miss short connections; no firewall guarantee.'}
        save({'phase': 'observation-end', **result})
    write(state / 'network-observation.json', result)
    message = {'forbidden-observed': '許可外の接続先あり', 'unknown': '未確認（親が停止/状態を確認）',
               'none-observed': '観測窓では許可外の接続先なし（短い通信は見逃し得る）'}
    print('OS観測: ' + message[result['verdict']], flush=True)
    return 1 if forbidden else 2 if errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    args = parser.parse_args()
    try:
        return observe(args.state)
    except Exception:
        print('OS観測: 準備失敗。起動せずstateを保持してください。', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
