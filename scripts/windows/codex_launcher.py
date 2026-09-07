"""Experimental pre-registered Codex child in an exclusively owned tmux server.

The caller supplies a prepared, private child home. Registration and task Mail
belong to the caller (PR2); this module never enables Dashboard SPAWN.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit

import psutil

from private_state import consume_token, create_private_directory, require_private
from owned_job import OwnedJob

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
    temporary.replace(path)


def executable(value: str) -> str:
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or path.suffix.lower() != '.exe':
        raise ValueError('Runtime must be an existing absolute .exe path')
    return str(path.resolve())


def process_record(process: psutil.Process) -> dict:
    return {'pid': process.pid, 'created': process.create_time(), 'exe': process.exe()}


def matching_process(record: dict) -> psutil.Process | None:
    try:
        process = psutil.Process(record['pid'])
        if (process.create_time() == record['created']
                and os.path.normcase(process.exe()) == os.path.normcase(record['exe'])):
            return process
    except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
        pass
    return None


def stop_owned(records: list[dict]) -> list[int]:
    """PID reuse must never turn cleanup into termination of another process."""
    owned = {}
    for record in records:
        parent = matching_process(record)
        if parent is None:
            continue
        try:
            for child in parent.children(recursive=True):
                owned[child.pid] = process_record(child)
            owned[parent.pid] = record
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    stopped = []
    for record in reversed(list(owned.values())):
        process = matching_process(record)
        if process:
            try:
                process.terminate()
                stopped.append(process.pid)
            except psutil.NoSuchProcess:
                pass
    survivors = [p for r in owned.values() if (p := matching_process(r))]
    _, alive = psutil.wait_procs(survivors, timeout=5)
    if alive:
        raise RuntimeError('Owned processes did not exit; state retained for recovery')
    return stopped


def tmux(spec: dict, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([spec['tmux'], '-S', spec['socket'], *arguments],
                          capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=10, check=check)


def pane_ready(text: str) -> bool:
    # Recognize an idle prompt only; startup, trust and permission dialogs
    # require the human. Do not inject a Mail task into an unknown screen.
    # Codex leaves old startup notifications in scrollback; match its current
    # footer like the canonical shell helper rather than blocking on history.
    tail = '\n'.join(line for line in text.splitlines() if line.strip()).splitlines()[-3:]
    current = '\n'.join(tail)
    lower = current.lower()
    if any(marker in lower for marker in (
            'starting mcp', 'startup completes', 'sign in', 'trust this',
            'set up', 'setup', 'would you like', 'approve', 'usage limit reached')):
        return False
    return bool(re.search(r'^\s*[›❯>]\s+\S', current, re.MULTILINE)
                and re.search(r'gpt-[\w.\-]+', current))


def configure_proxy(home: Path, spec: dict) -> None:
    """Explicit child-only config, never copy an unbound Mail MCP connection."""
    # The prepared home must not have a configuration. Its login/sandbox setup
    # can be provisioned by the caller, but configuration ownership is explicit.
    target = home / 'config.toml'
    if target.exists():
        raise ValueError('Child home config.toml already exists; use a dedicated prepared home')
    quote = json.dumps
    lines = [f'model = {quote(spec["model"])}',
             f'model_reasoning_effort = {quote(spec["effort"])}',
             '[windows]', 'sandbox = "elevated"',
             '[mcp_servers.orrery-mail]',
             f'command = {quote(spec["python"])}',
             'args = ' + json.dumps(['-X', 'utf8', str(HERE / 'run_codex_proxy.py')]),
             'required = true', '[mcp_servers.orrery-mail.env]']
    values = {
        'PYTHONPATH': str(ROOT / 'integrations/codex_app/src'),
        'AGENTSTACK_PYTHON': spec['python'],
        'AGENTSTACK_PROXY_AGENT_NAME': spec['name'],
        'AGENTSTACK_PROXY_PROGRAM': 'codex',
        'AGENTSTACK_PROXY_TOKEN_FILE': str(Path(spec['state']) / 'owner.token'),
        'AGENTSTACK_PROJECT_KEY': spec['project'],
        'AGENTSTACK_MCP_URL': spec['mail_url'],
        'AGENTSTACK_MAIL_HTTP_BEARER_MODE': spec['bearer_mode'],
        'AGENTSTACK_MAIL_ENV': spec['mail_env'],
        'AGENTSTACK_RUNTIME_DIR': spec['state'],
        'AGENTSTACK_CODEX_APP_RUNTIME_DIR': str(home / 'proxy-runtime'),
    }
    lines.extend(f'{key} = {quote(value)}' for key, value in values.items())
    for tool in ('bootstrap', 'fetch_inbox', 'send_message', 'acknowledge_message',
                 'reserve_files', 'renew_reservations', 'release_reservations', 'runtime_status'):
        lines.extend([f'[mcp_servers.orrery-mail.tools.{tool}]', 'approval_mode = "approve"'])
    with target.open('x', encoding='utf-8') as stream:
        stream.write('\n'.join(lines) + '\n')


_SCRUBBED_CHILD_ENV = (
    'OPENAI_API_KEY', 'MCP_AGENT_MAIL_TOKEN', 'HTTP_BEARER_TOKEN',
    'AGENT_NAME', 'PARENT_AGENT', 'PROJECT_KEY',
    'AGENTSTACK_RESERVED_IDENTITY', 'AGENTSTACK_PROXY_AGENT_NAME',
    'AGENTSTACK_PROXY_TOKEN_FILE', 'AGENTSTACK_PROXY_PROGRAM',
    'AGENTSTACK_REGISTRATION_TOKEN', 'CHILD_REGISTRATION_TOKEN',
    'AGENTSTACK_SESSION_ID', 'AGENTSTACK_MAIL_ENV',
    'AGENTSTACK_MCP_URL', 'AGENTSTACK_CODEX_APP_RUNTIME_DIR',
)


def child_environment(spec: dict, inherited: dict[str, str] | None = None) -> dict[str, str]:
    """Build a child environment without inheriting parent credentials."""

    env = (os.environ if inherited is None else inherited).copy()
    for key in _SCRUBBED_CHILD_ENV:
        env.pop(key, None)
    env.update(CODEX_HOME=spec['home'], CODEX_SHARED_CODEX_DIR=spec['home'],
               PATH=spec['path'],
               AGENT_NAME=spec['name'], PARENT_AGENT=spec['parent'],
               AGENTSTACK_RESERVED_IDENTITY='1',
               AGENTSTACK_PROJECT_KEY=spec['project'],
               AGENTSTACK_CODEX_BIN=spec['codex'], AGENTSTACK_PYTHON=spec['python'])
    return env


def child(spec_path: Path) -> int:
    require_private(spec_path)
    spec = json.loads(spec_path.read_text(encoding='utf-8'))
    state = Path(spec['state'])
    record_path = state / 'processes.json'
    records = [process_record(psutil.Process())]
    write_json(record_path, {'processes': records, 'status': 'starting'})
    env = child_environment(spec)
    command = [spec['codex'], '-C', spec['cwd'], '--sandbox', 'workspace-write',
               '--ask-for-approval', spec['approval'], '--model', spec['model'],
               '-c', f'model_reasoning_effort="{spec["effort"]}"']
    process = None
    job = OwnedJob()
    try:
        process = job.start(command, env=env, cwd=spec['cwd'])
        records.append(process_record(psutil.Process(process.pid)))
        write_json(record_path, {'processes': records, 'status': 'running'})
        return process.wait()
    finally:
        # Closing also terminates orphaned MCP children after Codex has exited.
        job.close()
        # Do not include ourselves: the PowerShell script exits when we return.
        stop_owned(records[1:])
        (state / 'owner.token').unlink(missing_ok=True)
        write_json(record_path, {'processes': records, 'status': 'exited'})


def stop(state: Path) -> dict:
    require_private(state)
    records = []
    for filename in ('server.json', 'processes.json'):
        path = state / filename
        if path.exists():
            require_private(path)
            records.extend(json.loads(path.read_text(encoding='utf-8'))['processes'])
    stopped = stop_owned(records)
    (state / 'owner.token').unlink(missing_ok=True)
    write_json(state / 'result.json', {'ok': True, 'status': 'stopped', 'pids': stopped})
    return {'ok': True, 'status': 'stopped', 'pids': stopped}


def launch(args: argparse.Namespace) -> dict:
    if sys.platform != 'win32':
        raise RuntimeError('This experimental launcher requires native Windows')
    if not re.fullmatch(r'[A-Z][A-Za-z]{1,63}(?:-[A-Z][A-Za-z]{1,63})?', args.name):
        raise ValueError('Invalid pre-registered child name')
    if not re.fullmatch(r'[A-Z][A-Za-z]{1,63}(?:-[A-Z][A-Za-z]{1,63})?', args.parent):
        raise ValueError('A valid parent is required; standalone launch is outside PR1')
    endpoint = urlsplit(args.mail_url)
    if (endpoint.scheme != 'http' or endpoint.hostname not in ('127.0.0.1', '::1', 'localhost')
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
        raise ValueError('This launcher requires an explicit loopback HTTP Mail endpoint')
    if not args.project.strip() or any(ord(char) < 32 for char in args.project):
        raise ValueError('Project key must be non-empty and contain no control characters')
    if not math.isfinite(args.ready_timeout) or not 1 <= args.ready_timeout <= 300:
        raise ValueError('Ready timeout must be between 1 and 300 seconds')
    tmux_path = shutil.which('tmux')
    if not tmux_path:
        raise RuntimeError('Native tmux is missing; install a tested distribution separately')
    codex = executable(args.codex)
    python = executable(args.python)
    cwd = Path(args.cwd).resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError('Working directory must be a directory')
    home = Path(args.codex_home).absolute()
    if not home.is_dir():
        raise ValueError('Prepared child home must be an existing directory')
    require_private(home)
    if (home / 'config.toml').exists():
        raise ValueError('Prepared child home must not contain config.toml')
    mail_env = None
    if args.mail_env:
        mail_env = Path(args.mail_env).absolute()
        if not mail_env.is_file():
            raise ValueError('Mail env must be an existing private file')
        require_private(mail_env)
    elif args.bearer_mode == 'enabled':
        raise ValueError('Authenticated Mail requires --mail-env or AGENTSTACK_MAIL_ENV')
    parent_state = Path(args.state_directory).absolute()
    create_private_directory(parent_state)
    state = parent_state / uuid.uuid4().hex
    create_private_directory(state)
    spec = dict(name=args.name, parent=args.parent, cwd=str(cwd), project=args.project,
                codex=codex, python=python, tmux=tmux_path,
                socket='orrery-' + uuid.uuid4().hex, home=str(home), state=str(state),
                model=args.model, effort=args.effort, approval=args.approval,
                mail_url=args.mail_url, mail_env=str(mail_env) if mail_env else '',
                bearer_mode=args.bearer_mode,
                path=os.environ.get('PATH', ''))
    spec_path = state / 'launch.json'
    write_json(spec_path, spec)
    try:
        configure_proxy(home, spec)
        consume_token(Path(args.child_token_file).absolute(), state / 'owner.token')
        # Own the server process before any client command: even an unresponsive
        # named pipe leaves an exact PID/create-time record for cleanup.
        with (state / 'tmux.log').open('ab') as log:
            server = subprocess.Popen([tmux_path, '-D', '-S', spec['socket'], '-f', 'NUL'],
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        write_json(state / 'server.json', {'processes': [process_record(psutil.Process(server.pid))]})
        # tmux passes one command through a shell. Encode the fixed invocation
        # after quoting PS literals so $, backticks and quotes in paths stay data.
        literal = lambda value: "'" + str(value).replace("'", "''") + "'"
        script = ('& ' + literal(HERE / 'run-codex-child.ps1')
                  + ' -Python ' + literal(python) + ' -SpecFile ' + literal(spec_path))
        encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        command = subprocess.list2cmdline([
            shutil.which('powershell.exe'), '-NoLogo', '-NoProfile', '-EncodedCommand', encoded])
        # Wait for the owned server to publish its endpoint. Clients must never
        # auto-create a second server if initialization failed.
        started = False
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.WaitNamedPipeW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong]
        kernel.WaitNamedPipeW.restype = ctypes.c_int
        for _ in range(50):
            if server.poll() is not None:
                raise RuntimeError('Owned tmux server exited during startup')
            if kernel.WaitNamedPipeW('\\\\.\\pipe\\' + spec['socket'], 1):
                started = True
                break
            time.sleep(0.1)
        if not started:
            raise RuntimeError('Owned tmux server did not expose its endpoint')
        tmux(spec, 'new-session', '-d', '-s', args.name,
             '-x', '120', '-y', '35', command,
             ';', 'set-option', '-w', '-t', args.name, 'remain-on-exit', 'on')
        pid = int(tmux(spec, 'display-message', '-p', '#{pid}').stdout.strip())
        if pid != server.pid:
            raise RuntimeError('tmux server identity changed during startup')
        tmux(spec, 'set-option', '-s', 'exit-empty', 'on')
        print(json.dumps({'status': 'starting', 'state_directory': str(state),
                          'tmux_socket': spec['socket'], 'session': args.name}),
              file=sys.stderr, flush=True)
        deadline = time.monotonic() + args.ready_timeout
        while time.monotonic() < deadline:
            pane = tmux(spec, 'capture-pane', '-p', '-t', args.name).stdout
            if tmux(spec, 'display-message', '-p', '-t', args.name, '#{pane_dead}').stdout.strip() == '1':
                (state / 'startup-pane.txt').write_text(pane, encoding='utf-8')
                raise RuntimeError('Child exited before readiness; see private startup-pane.txt')
            if pane_ready(pane):
                time.sleep(2)
                if not pane_ready(tmux(spec, 'capture-pane', '-p', '-t', args.name).stdout):
                    continue
                tmux(spec, 'set-option', '-w', '-t', args.name, 'remain-on-exit', 'off')
                prompt = (f'You are {args.name}; your parent is {args.parent}. Your identity is already '
                          'registered; do not register a different name. Use the orrery-mail MCP '
                          f'server fetch_inbox tool for project {json.dumps(args.project)} to read '
                          'the canonical task, then use its send_message tool to reply to your parent. '
                          'The proxy owns your token; never request or print it. These tools connect '
                          'to ORRERY Mail directly; agmsg is a separate system and is not used for this task.')
                tmux(spec, 'send-keys', '-t', args.name, '-l', prompt)
                time.sleep(0.5)
                tmux(spec, 'send-keys', '-t', args.name, 'C-m')
                result = {'ok': True, 'status': 'submitted', 'child_name': args.name,
                          'state_directory': str(state), 'tmux_socket': spec['socket']}
                write_json(state / 'result.json', result)
                return result
            time.sleep(0.5)
        (state / 'startup-pane.txt').write_text(pane, encoding='utf-8')
        raise TimeoutError('Codex did not reach a recognized prompt; task was not submitted')
    except Exception as error:
        cleanup_error = None
        try:
            stop(state)
        except Exception as cleanup:
            cleanup_error = type(cleanup).__name__
        result = {'ok': False, 'error': str(error), 'child_name': args.name,
                  'registration_retained': True, 'state_directory': str(state),
                  'cleanup_error': cleanup_error}
        write_json(state / 'result.json', result)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    start = sub.add_parser('launch')
    for name in ('name', 'parent', 'cwd', 'project', 'codex-home', 'state-directory',
                 'child-token-file', 'mail-url', 'model'):
        start.add_argument('--' + name, required=True)
    start.add_argument('--codex', default=os.environ.get('AGENTSTACK_CODEX_BIN', ''))
    start.add_argument('--python', default=os.environ.get('AGENTSTACK_PYTHON', sys.executable))
    start.add_argument('--effort', choices=('low', 'medium', 'high', 'xhigh', 'max'), default='xhigh')
    start.add_argument('--approval', choices=('never', 'on-request', 'untrusted'), default='never')
    start.add_argument('--mail-env', default=os.environ.get('AGENTSTACK_MAIL_ENV', ''))
    start.add_argument('--bearer-mode', choices=('enabled', 'disabled'), default='enabled')
    start.add_argument('--ready-timeout', type=float, default=90)
    sub.add_parser('stop').add_argument('--state-directory', required=True)
    sub.add_parser('_child').add_argument('--spec-file', required=True)
    args = parser.parse_args()
    if args.action == '_child':
        return child(Path(args.spec_file))
    try:
        result = stop(Path(args.state_directory)) if args.action == 'stop' else launch(args)
    except Exception as error:
        result = {'ok': False, 'error': str(error)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
