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
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import psutil
import tomllib
from owned_job import OwnedJob
from private_state import (
    consume_token,
    create_private_directory,
    require_private,
    write_private_text,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def write_json(path: Path, data: dict) -> None:
    write_private_text(path, json.dumps(data, ensure_ascii=False, indent=2))


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


def trust_dialog_present(text: str) -> bool:
    """Recognize Codex's directory-trust dialog before treating a pane as idle."""
    lower = text.lower()
    return ('do you trust the contents of this directory?' in lower
            and '1. yes, continue' in lower
            and '2. no, quit' in lower)


def pane_ready(text: str) -> bool:
    # Recognize an idle prompt only; startup, trust and permission dialogs
    # require the human. Do not inject a Mail task into an unknown screen.
    # Codex leaves old startup notifications in scrollback; match its current
    # footer like the canonical shell helper rather than blocking on history.
    lines = [line for line in text.splitlines() if line.strip()]
    current = '\n'.join(lines[-2:])
    nearby = '\n'.join(lines[-3:]).lower()
    if trust_dialog_present(text) or any(marker in nearby for marker in (
            'startup completes', 'sign in', 'set up', 'setup', 'would you like',
            'approve', 'usage limit reached')):
        return False
    return bool(re.search(r'^\s*[›❯>]\s+\S', current, re.MULTILINE)
                and re.search(r'gpt-[\w.\-]+', current))


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


_HOME_LOCK_NAME = '.orrery-launch.lock'


def _state_spec(state: Path) -> dict | None:
    launch = state / 'launch.json'
    if not launch.is_file():
        return None
    try:
        require_private(state)
        require_private(launch)
        spec = json.loads(launch.read_text(encoding='utf-8'))
    except (OSError, PermissionError, json.JSONDecodeError):
        return None
    return spec if isinstance(spec, dict) else None


def _state_claims_home(state: Path, home: Path) -> bool:
    spec = _state_spec(state)
    return (spec is not None and isinstance(spec.get('home'), str)
            and isinstance(spec.get('state'), str)
            and _same_path(Path(spec['home']), home)
            and _same_path(Path(spec['state']), state))


def _home_lock_path(home: Path) -> Path:
    return home / _HOME_LOCK_NAME


def _home_lock_data(home: Path) -> dict | None:
    lock = _home_lock_path(home)
    if not lock.is_file():
        return None
    try:
        require_private(lock)
        data = json.loads(lock.read_text(encoding='utf-8'))
    except (OSError, PermissionError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def release_home_lock(state: Path) -> bool:
    """Release only the home claim recorded by this launch state."""
    spec = _state_spec(state)
    if spec is None or not isinstance(spec.get('home'), str):
        return False
    home = Path(spec['home']).absolute()
    lock = _home_lock_path(home)
    data = _home_lock_data(home)
    if data is None or not isinstance(data.get('state'), str):
        return False
    if not _same_path(Path(data['state']), state):
        return False
    lock.unlink()
    return True


def acquire_home_lock(home: Path, state: Path) -> None:
    """Atomically claim a prepared CODEX_HOME for one launcher process."""
    lock = _home_lock_path(home)
    payload = {
        'home': str(home.absolute()),
        'state': str(state.absolute()),
        'owner': process_record(psutil.Process()),
    }
    try:
        write_private_text(lock, json.dumps(payload, ensure_ascii=False), exclusive=True)
        return
    except FileExistsError:
        pass
    data = _home_lock_data(home)
    if (data is None or not isinstance(data.get('state'), str)
            or not _state_claims_home(Path(data['state']).absolute(), home)):
        raise RuntimeError('Prepared child home is already claimed by an unknown launcher')
    owner = data.get('owner')
    if isinstance(owner, dict) and matching_process(owner) is not None:
        raise RuntimeError('Prepared child home is already in use by an active launcher')
    stale_state = Path(data['state']).absolute()
    if state_has_live_processes(stale_state):
        raise RuntimeError('Prepared child home is already in use by an active launcher state')
    stop(stale_state)
    try:
        write_private_text(lock, json.dumps(payload, ensure_ascii=False), exclusive=True)
    except FileExistsError as error:
        raise RuntimeError('Prepared child home became claimed by another launcher') from error


def managed_config_state(home: Path) -> Path | None:
    """Return the state that owns this launcher's config, never a user config."""
    target = home / 'config.toml'
    if not target.is_file():
        return None
    try:
        require_private(target)
        config = tomllib.loads(target.read_text(encoding='utf-8'))
        environment = config['mcp_servers']['orrery-mail']['env']
        runtime_directory = environment['AGENTSTACK_RUNTIME_DIR']
        state = Path(runtime_directory).absolute()
        launch = state / 'launch.json'
        require_private(state)
        require_private(launch)
        spec = json.loads(launch.read_text(encoding='utf-8'))
    except (KeyError, OSError, PermissionError, tomllib.TOMLDecodeError, json.JSONDecodeError):
        return None
    recorded_home = spec.get('home')
    recorded_state = spec.get('state')
    if not (isinstance(recorded_home, str) and isinstance(recorded_state, str)
            and _same_path(Path(recorded_home), home)
            and _same_path(Path(recorded_state), state)):
        return None
    return state


def remove_managed_config(state: Path) -> bool:
    """Remove only the config that this exact launch state created."""
    launch = state / 'launch.json'
    if not launch.is_file():
        return False
    require_private(launch)
    spec = json.loads(launch.read_text(encoding='utf-8'))
    home = Path(spec['home'])
    managed_state = managed_config_state(home)
    if managed_state is None or not _same_path(managed_state, state):
        return False
    (home / 'config.toml').unlink()
    return True


def state_has_live_processes(state: Path) -> bool:
    """Use recorded identities, never a bare PID, when deciding whether a home is busy."""
    for filename in ('server.json', 'processes.json'):
        record_path = state / filename
        if not record_path.is_file():
            continue
        require_private(record_path)
        records = json.loads(record_path.read_text(encoding='utf-8')).get('processes', [])
        if any(matching_process(record) is not None for record in records):
            return True
    return False


def recover_stale_managed_config(home: Path, state_root: Path) -> bool:
    """Recover only a stopped launcher's own config; leave user config untouched."""
    state = managed_config_state(home)
    if state is None:
        return False
    try:
        common = os.path.commonpath((os.path.abspath(state), os.path.abspath(state_root)))
    except ValueError:
        return False
    if not _same_path(Path(common), state_root):
        return False
    lock_data = _home_lock_data(home)
    if (lock_data is not None and isinstance(lock_data.get('owner'), dict)
            and matching_process(lock_data['owner']) is not None):
        raise RuntimeError('Prepared child home is already in use by an active launcher')
    if state_has_live_processes(state):
        raise RuntimeError('Prepared child home is already in use by an active launcher state')
    stop(state)
    return True


def request_directory_trust(cwd: Path, input_fn=input, output=None) -> bool:
    """Bring Codex's directory-trust decision back to the launching console."""
    stream = sys.stderr if output is None else output
    print(f'Codex requests trust for {cwd}. Type yes to continue, or anything else to cancel:',
          file=stream, flush=True)
    return input_fn().strip().casefold() == 'yes'


def configure_proxy(home: Path, spec: dict) -> None:
    """Explicit child-only config, never copy an unbound Mail MCP connection."""
    # The prepared home must not have a configuration. Its login/sandbox setup
    # can be provisioned by the caller, but configuration ownership is explicit.
    require_private(home)
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
    # Keep in step with TOOL_DEFINITIONS in integrations/codex_app (nine tools
    # since `whois`, 58525ed); tests/test_child_mcp_config.py pins the same list.
    for tool in ('bootstrap', 'fetch_inbox', 'send_message', 'acknowledge_message',
                 'reserve_files', 'renew_reservations', 'release_reservations', 'runtime_status',
                 'whois'):
        lines.extend([f'[mcp_servers.orrery-mail.tools.{tool}]', 'approval_mode = "approve"'])
    write_private_text(target, '\n'.join(lines) + '\n', exclusive=True)


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
               '-c', f'model_reasoning_effort="{spec["effort"]}"',
               # No startup update screen in an unattended child (#60).
               '-c', 'check_for_update_on_startup=false']
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
        remove_managed_config(state)
        release_home_lock(state)
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
    config_removed = remove_managed_config(state)
    lock_removed = release_home_lock(state)
    result = {'ok': True, 'status': 'stopped', 'pids': stopped,
              'config_removed': config_removed, 'lock_removed': lock_removed}
    write_json(state / 'result.json', result)
    return result


def console_is_interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, OSError):
        return False


def task_prompt(spec: dict) -> str:
    return (f'You are {spec["name"]}; your parent is {spec["parent"]}. Your identity is already '
            'registered; do not register a different name. Use the orrery-mail MCP '
            f'server fetch_inbox tool for project {json.dumps(spec["project"])} to read '
            'the canonical task, then use its send_message tool to reply to your parent. '
            'The proxy owns your token; never request or print it. These tools connect '
            'to ORRERY Mail directly; agmsg is a separate system and is not used for this task.')


def trust_required_result(spec: dict, state: Path, pane: str) -> dict:
    write_private_text(state / 'startup-pane.txt', pane)
    result = {
        'ok': False,
        'status': 'trust_required',
        'error': ('Codex is waiting for directory trust; attach to the recorded tmux session, '
                  'choose Yes, continue, then run resume with this state directory'),
        'child_name': spec['name'],
        'registration_retained': True,
        'state_directory': str(state),
        'tmux_socket': spec['socket'],
        'session': spec['name'],
    }
    write_json(state / 'result.json', result)
    return result


def wait_until_ready_and_submit(spec: dict, state: Path, ready_timeout: float) -> dict:
    """Wait for a usable Codex pane, handling trust in either console mode."""
    deadline = time.monotonic() + ready_timeout
    trust_approved_at = None
    pane = ''
    while time.monotonic() < deadline:
        pane = tmux(spec, 'capture-pane', '-p', '-t', spec['name']).stdout
        if tmux(spec, 'display-message', '-p', '-t', spec['name'], '#{pane_dead}').stdout.strip() == '1':
            write_private_text(state / 'startup-pane.txt', pane)
            raise RuntimeError('Child exited before readiness; see private startup-pane.txt')
        if trust_dialog_present(pane):
            if not console_is_interactive():
                return trust_required_result(spec, state, pane)
            if trust_approved_at is None:
                if not request_directory_trust(Path(spec['cwd'])):
                    raise RuntimeError('Directory trust was not granted')
                tmux(spec, 'send-keys', '-t', spec['name'], '1')
                tmux(spec, 'send-keys', '-t', spec['name'], 'C-m')
                trust_approved_at = time.monotonic()
            elif time.monotonic() - trust_approved_at >= 10:
                raise RuntimeError('Codex did not accept the directory-trust confirmation')
            time.sleep(0.5)
            continue
        if pane_ready(pane):
            time.sleep(2)
            if not pane_ready(tmux(spec, 'capture-pane', '-p', '-t', spec['name']).stdout):
                continue
            tmux(spec, 'set-option', '-w', '-t', spec['name'], 'remain-on-exit', 'off')
            tmux(spec, 'send-keys', '-t', spec['name'], '-l', task_prompt(spec))
            time.sleep(0.5)
            tmux(spec, 'send-keys', '-t', spec['name'], 'C-m')
            result = {'ok': True, 'status': 'submitted', 'child_name': spec['name'],
                      'state_directory': str(state), 'tmux_socket': spec['socket']}
            write_json(state / 'result.json', result)
            return result
        time.sleep(0.5)
    write_private_text(state / 'startup-pane.txt', pane)
    raise TimeoutError('Codex did not reach a recognized prompt; task was not submitted')


def resume(state: Path, ready_timeout: float) -> dict:
    """Continue a non-interactive launch after its trust decision is made."""
    if not math.isfinite(ready_timeout) or not 1 <= ready_timeout <= 300:
        raise ValueError('Ready timeout must be between 1 and 300 seconds')
    require_private(state)
    spec = _state_spec(state)
    if spec is None or not isinstance(spec.get('state'), str) or not _same_path(Path(spec['state']), state):
        raise ValueError('Launch state is missing or does not identify itself')
    if not isinstance(spec.get('home'), str) or not _state_claims_home(state, Path(spec['home']).absolute()):
        raise ValueError('Launch state has an invalid child home')
    home = Path(spec['home']).absolute()
    require_private(home)
    lock_data = _home_lock_data(home)
    if (lock_data is None or not isinstance(lock_data.get('state'), str)
            or not _same_path(Path(lock_data['state']), state)):
        raise ValueError('Launch state no longer owns the prepared child home')
    if not (state / 'owner.token').is_file():
        raise ValueError('Launch state no longer has an owner token')
    try:
        return wait_until_ready_and_submit(spec, state, ready_timeout)
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - CLI boundary must report every resume failure
        cleanup_error = None
        try:
            stop(state)
        except Exception as cleanup:  # noqa: BLE001 - preserve the original resume failure
            cleanup_error = type(cleanup).__name__
        result = {
            'ok': False,
            'error': 'Cancelled by user' if isinstance(error, KeyboardInterrupt) else str(error),
            'child_name': spec.get('name', ''),
            'registration_retained': True,
            'state_directory': str(state),
            'cleanup_error': cleanup_error,
        }
        write_json(state / 'result.json', result)
        return result


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
    parent_state = Path(args.state_directory).absolute()
    create_private_directory(parent_state)
    if (home / 'config.toml').exists():
        recover_stale_managed_config(home, parent_state)
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
    handoff = Path(args.child_token_file).absolute()
    if not handoff.is_file():
        raise ValueError('Child token handoff must be an existing private file')
    require_private(handoff)
    state = parent_state / uuid.uuid4().hex
    create_private_directory(state)
    spec = {'name': args.name, 'parent': args.parent, 'cwd': str(cwd),
            'project': args.project, 'codex': codex, 'python': python,
            'tmux': tmux_path, 'socket': 'orrery-' + uuid.uuid4().hex,
            'home': str(home), 'state': str(state), 'model': args.model,
            'effort': args.effort, 'approval': args.approval,
            'mail_url': args.mail_url,
            'mail_env': str(mail_env) if mail_env else '',
            'bearer_mode': args.bearer_mode,
            'path': os.environ.get('PATH', '')}
    spec_path = state / 'launch.json'
    write_json(spec_path, spec)
    try:
        acquire_home_lock(home, state)
        configure_proxy(home, spec)
        consume_token(handoff, state / 'owner.token')
        # Own the server process before any client command: even an unresponsive
        # named pipe leaves an exact PID/create-time record for cleanup.
        tmux_log = state / 'tmux.log'
        write_private_text(tmux_log, '', exclusive=True)
        with tmux_log.open('ab') as log:
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
        return wait_until_ready_and_submit(spec, state, args.ready_timeout)
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - CLI boundary must report every launch failure
        cleanup_error = None
        try:
            stop(state)
        except Exception as cleanup:  # noqa: BLE001 - preserve the original launch failure
            cleanup_error = type(cleanup).__name__
        result = {'ok': False,
                  'error': 'Cancelled by user' if isinstance(error, KeyboardInterrupt) else str(error),
                  'child_name': args.name,
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
    resume_parser = sub.add_parser('resume')
    resume_parser.add_argument('--state-directory', required=True)
    resume_parser.add_argument('--ready-timeout', type=float, default=90)
    sub.add_parser('_child').add_argument('--spec-file', required=True)
    args = parser.parse_args()
    if args.action == '_child':
        return child(Path(args.spec_file))
    try:
        if args.action == 'stop':
            result = stop(Path(args.state_directory))
        elif args.action == 'resume':
            result = resume(Path(args.state_directory), args.ready_timeout)
        else:
            result = launch(args)
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - CLI boundary must return JSON
        result = {'ok': False,
                  'error': 'Cancelled by user' if isinstance(error, KeyboardInterrupt) else str(error)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
