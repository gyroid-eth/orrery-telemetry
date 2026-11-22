"""Ephemeral tmux transports and legacy target admission from existing truth.

No socket registry, mode selector, SQLite reads or new client journal. The CLI
is deliberately useful to an older consumer before global launch is enabled.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import shlex
import socket
import stat
import subprocess
import time
import threading

_MODULES = {}
_SETTINGS = {}
_CACHE_LOCK = threading.RLock()


def module_api(name):
    """Load code once per consumer process; placement/live data stays fresh."""
    with _CACHE_LOCK:
        if name not in _MODULES:
            path = Path(__file__).with_name(name + ".py")
            if name == "terminal_state" and not path.is_file():
                path = Path(__file__).resolve().parents[2]/"dashboard/terminal_state.py"
            _MODULES[name] = runpy.run_path(str(path))
        return _MODULES[name]

class RuntimeAPIView:
    """Load the same client functions only when the observation needs them."""
    def __getitem__(self,name):
        module='process_observation' if name in ('process_start','integer','remaining','ClientError') else 'runtime_client'
        return module_api(module)[name]


def runtime_api_view():
    return RuntimeAPIView()


SPEC = json.loads(Path(__file__).with_name('runtime_tmux_contract.json').read_text())
CONTRACT = SPEC['contract']
CAPABILITIES = SPEC['capabilities']
OPERATIONS = tuple(SPEC['operations'])
REASONS = SPEC['reasons']


class TargetError(RuntimeError):
    pass


def fresh_host_path(context):
    """Derive a short same-user endpoint; the context remains on the host."""
    path = Path(context)
    if not path.is_absolute() or path.name != 'context.json' or path.resolve() != path:
        raise TargetError('FRESH_CONTEXT_REQUIRED')
    base = Path('/tmp').resolve(strict=True)
    name = hashlib.sha256(os.fsencode(path)).hexdigest()[:24]
    return base / ('orrery-target-%s-%s' % (os.getuid(), name)) / 'host.sock'


def fresh_host_identity(path):
    path = Path(path)
    parent = path.parent.lstat()
    info = path.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077
            or not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise TargetError('FRESH_HOST_UNSAFE')
    return (parent.st_dev, parent.st_ino, info.st_dev, info.st_ino)


def fresh_host_call(path, payload, *, expected_socket=None):
    raw = json.dumps(payload, separators=(',', ':'), allow_nan=False).encode() + b'\n'
    if len(raw) > 16384:
        raise TargetError('FRESH_TARGET_QUERY_INVALID')
    try:
        identity = fresh_host_identity(path)
        if expected_socket is not None and identity != expected_socket:
            raise TargetError('FRESH_HOST_CHANGED')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(7)
            conn.connect(os.fspath(path))
            if fresh_host_identity(path) != identity:
                raise TargetError('FRESH_HOST_CHANGED')
            conn.sendall(raw)
            chunks = bytearray()
            while len(chunks) <= 262144 and b'\n' not in chunks:
                chunk = conn.recv(min(8192, 262145-len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
            if len(chunks) > 262144 or not chunks.endswith(b'\n'):
                raise TargetError('FRESH_HOST_RESPONSE_INVALID')
            def unique(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:raise ValueError('duplicate key')
                    value[key] = item
                return value
            value = json.loads(chunks, object_pairs_hook=unique)
            if not isinstance(value, dict) or fresh_host_identity(path) != identity:
                raise TargetError('FRESH_HOST_RESPONSE_INVALID')
            return identity, value
    except (OSError, ValueError, TypeError) as exc:
        raise TargetError('FRESH_HOST_UNAVAILABLE') from exc


def result_schema(name):
    fields = SPEC['result_fields'][name]
    properties = {key: SPEC['result_properties'][key] for key in fields}
    if 'reason' in properties:properties['reason']={'enum':list(REASONS)}
    if 'capabilities' in properties:properties['capabilities']={'const':CAPABILITIES}
    if 'ok' in properties:
        properties['ok'] = {'const': name != 'error'}
    if 'allowed' in properties:
        properties['allowed'] = {'const': name != 'error'}
    if name == 'legacy-target':
        properties['server_pid'] = {'type':'integer','minimum':1}
    result = {'type':'object','additionalProperties':False,'required':fields,'properties':properties}
    if name == 'error':
        return {'anyOf':[{**result,'properties':{**properties,
            'reason':{'enum':[r for r,k in REASONS.items() if k==kind]},
            'failure_kind':{'const':kind}}} for kind in ('target','transport','helper')]}
    return result


def print_result(value, name):
    root = Path(__file__).resolve().parents[2]
    installed = Path(__file__).with_name('schema_contract.py')
    validator = runpy.run_path(str(installed if installed.is_file() else
        root / 'packages/agentstack_mail/src/agentstack_mail/schema_contract.py'))
    validator['validate_schema'](value, result_schema(name))
    print(json.dumps(value, ensure_ascii=False))


def check_pinned_transport(pinned, reply):
    """Compare validated replies; the returned pin lives only in consumer memory."""
    if any(pinned[key] != reply[key] for key in ('transport','socket_path','argv')):
        raise TargetError('TMUX_SOCKET_CHANGED')
    if pinned['server_pid'] is not None and pinned['server_pid'] != reply['server_pid']:
        raise TargetError('TMUX_SOCKET_CHANGED')
    return {**pinned,'server_pid':reply['server_pid']}


def lstat_if_present(path):
    return module_api('legacy_consumer')['lstat_if_present'](path)


def private_directory(path, *, create=False):
    path = Path(path)
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_mode & 0o700 != 0o700
            or path.resolve() != path):
        raise TargetError('TMUX_DIRECTORY_UNSAFE')
    return path


def global_socket(client_home, mail_instance_id, *, base=None, create=False):
    """One deterministic endpoint; ``base`` is an in-process fixture seam only."""
    home = Path(client_home)
    if not home.is_absolute() or home.resolve() != home:
        raise TargetError('TMUX_CLIENT_HOME_INVALID')
    if not isinstance(mail_instance_id, str) or not mail_instance_id:
        raise TargetError('TMUX_INSTANCE_REQUIRED')
    directory = (Path('/tmp').resolve() / ('orrery-tmux-' + str(os.getuid()))
                 if base is None else Path(base))
    digest = hashlib.sha256(json.dumps([str(home), mail_instance_id],
                                      separators=(',', ':')).encode()).hexdigest()
    path = directory / (digest + '.sock')
    # macOS sockaddr_un is the narrower supported boundary (104 bytes incl NUL).
    if len(os.fsencode(path)) >= 104:
        raise TargetError('TMUX_SOCKET_PATH_TOO_LONG')
    if create or lstat_if_present(directory) is not None:
        private_directory(directory, create=create)
    if lstat_if_present(path) is not None:
        validate_socket(path)
    return path


def validate_socket(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise TargetError('TMUX_SOCKET_UNSAFE')
    try:
        private_directory(path.parent)
        info = path.lstat()
    except OSError as exc:
        raise TargetError('TMUX_SOCKET_UNSAFE') from exc
    # tmux itself creates 0660 sockets. The UID-owned 0700 parent prevents
    # group traversal; never chmod somebody's existing server socket.
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o007 or info.st_mode & 0o600 != 0o600):
        raise TargetError('TMUX_SOCKET_UNSAFE')
    return info.st_dev, info.st_ino


class Transport:
    def __init__(self, socket_path, *, binary=None, run=None, allow_absent=False):
        self.path = Path(socket_path)
        self.binary = binary or shutil.which('tmux')
        if not self.binary:
            raise TargetError('TMUX_UNAVAILABLE')
        self.run = subprocess.run if run is None else run
        if allow_absent and lstat_if_present(self.path) is None:
            if not self.path.is_absolute() or self.path.resolve() != self.path:
                raise TargetError('TMUX_SOCKET_UNSAFE')
            if lstat_if_present(self.path.parent) is not None:
                private_directory(self.path.parent)
            self.identity = None
        else:
            self.identity = validate_socket(self.path)

    @property
    def argv(self):
        return [self.binary, '-S', str(self.path)]

    def command(self, *args, deadline=None, input=None):
        if self.identity is None:
            raise TargetError('TMUX_TARGET_UNAVAILABLE')
        if validate_socket(self.path) != self.identity:
            raise TargetError('TMUX_SOCKET_CHANGED')
        timeout = 5 if deadline is None else min(5, deadline - time.monotonic())
        if timeout <= 0:
            raise TargetError('TMUX_TARGET_UNAVAILABLE')
        env = {k: v for k, v in os.environ.items() if k not in ('TMUX', 'TMUX_PANE')}
        # Only the tmux client needs UTF-8; C clients sanitize TAB in formats.
        env.pop('LC_ALL', None)
        env['LC_CTYPE'] = 'C.UTF-8'
        try:
            result = self.run([*self.argv, *args], capture_output=True, text=True,
                              timeout=timeout, env=env, **({'input':input} if input is not None else {}))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TargetError('TMUX_TARGET_UNAVAILABLE') from exc
        if validate_socket(self.path) != self.identity:
            raise TargetError('TMUX_SOCKET_CHANGED')
        return result

    def public(self):
        # A delta, not inherited credentials or a second environment record.
        return {'transport': 'legacy-default', 'socket_path': str(self.path),
                'argv': self.argv, 'environment_unset': ['TMUX', 'TMUX_PANE'],
                'server_pid': getattr(self, 'server_pid', None)}


def legacy_endpoint(*, environ=None):
    """The socket path (and inherited server PID) legacy_transport resolves, with
    no process and no socket check. A caller that keeps a transport compares
    its socket with this path to know the environment still names it."""
    env = os.environ if environ is None else environ
    inherited = env.get('TMUX')
    server_pid = None
    if inherited:
        parts = inherited.rsplit(',', 2)
        if len(parts) != 3 or not parts[1].isdigit():
            raise TargetError('LEGACY_TMUX_TRANSPORT_UNAVAILABLE')
        path = Path(parts[0])
        server_pid = parts[1]
    else:
        base = Path(env.get('TMUX_TMPDIR') or '/tmp').resolve()
        path = base / ('tmux-' + str(os.getuid())) / 'default'
    # Resolve the /tmp alias, while retaining no-symlink validation below the
    # trusted temp directory. Do not silently resolve a substituted socket.
    if str(path).startswith('/tmp/'):
        path = Path('/tmp').resolve() / path.relative_to('/tmp')
    global_parent = Path('/tmp').resolve() / ('orrery-tmux-' + str(os.getuid()))
    if path.parent == global_parent:
        raise TargetError('GLOBAL_TMUX_LEGACY_CONSUMER_REFUSED')
    return path, server_pid


def legacy_transport(*, run=None, binary=None, environ=None):
    """Resolve once at consumer bootstrap; subsequent calls use explicit -S.

    An inherited custom endpoint must prove its exact socket/server PID. A
    dedicated global endpoint can never become a legacy consumer endpoint.
    """
    env = os.environ if environ is None else environ
    inherited = env.get('TMUX')
    path, server_pid = legacy_endpoint(environ=env)
    transport = Transport(path, binary=binary, run=run, allow_absent=not inherited)
    if transport.identity is None:
        # Read-only bootstrap of an actually absent default endpoint.
        return transport
    # tmux replaces control characters (including TAB) under a C-locale client.
    result = transport.command('display-message', '-p', '#{socket_path}|#{pid}')
    if module_api('legacy_consumer')['server_absent'](result):
        # A safe stale socket is an empty server, not an unknown transport.
        # Only inventory reads consume this result; targets still need proof.
        return transport
    fields = result.stdout.strip().rsplit('|', 1)
    if (result.returncode or len(fields) != 2 or not fields[1].isdigit()
            or Path(fields[0]) != path or (server_pid and fields[1] != server_pid)):
        raise TargetError('LEGACY_TMUX_TRANSPORT_UNAVAILABLE')
    transport.server_pid = int(fields[1])
    return transport


def panes(transport, *, session=None, pane=None, window=None, deadline=None):
    if sum(value is not None for value in (session,pane,window)) != 1:
        raise TargetError('TMUX_TARGET_INPUT_INVALID')
    if session is not None:
        if not isinstance(session, str) or not session or any(c in session for c in '\r\n\0:'):
            raise TargetError('TMUX_TARGET_INPUT_INVALID')
        args = ['list-panes', '-s', '-t', session if re.fullmatch(r'\$[0-9]+',session) else '=' + session]
    elif window is not None:
        if not isinstance(window,str) or re.fullmatch(r'@[0-9]+',window) is None:
            raise TargetError('TMUX_TARGET_INPUT_INVALID')
        args = ['list-panes', '-t', window]
    else:
        if not isinstance(pane, str) or re.fullmatch(r'%[0-9]+', pane) is None:
            raise TargetError('TMUX_TARGET_INPUT_INVALID')
        args = ['display-message', '-p', '-t', pane]
    fmt = '#{session_id}\t#{session_name}\t#{pane_id}\t#{pane_pid}'
    result = transport.command(*args, '-F', fmt, deadline=deadline) if pane is None else transport.command(*args, fmt, deadline=deadline)
    if result.returncode:
        raise TargetError('TMUX_TARGET_UNAVAILABLE')
    rows = []
    for line in result.stdout.splitlines():
        fields = line.split('\t')
        if (len(fields) != 4 or re.fullmatch(r'\$[0-9]+', fields[0]) is None
                or re.fullmatch(r'%[0-9]+', fields[2]) is None
                or not fields[3].isdigit() or int(fields[3]) < 1
                or (session is not None and (fields[0] if re.fullmatch(r'\$[0-9]+',session) else fields[1]) != session)
                or (pane is not None and fields[2] != pane)):
            raise TargetError('TMUX_TARGET_UNAVAILABLE')
        rows.append(dict(session_id=fields[0], session=fields[1], pane_id=fields[2], pid=int(fields[3])))
    if not rows:
        raise TargetError('TMUX_TARGET_UNAVAILABLE')
    return rows


def installed_hints(home, deadline):
    """Use the existing literal settings reader, without sourcing env.sh."""
    env_file = home / 'env.sh'
    if not env_file.exists() and not env_file.is_symlink():
        return {}
    reader = Path(__file__).resolve().parents[2] / 'hooks/project-context.sh'
    stamps = []
    for path in (reader, env_file):
        info = path.lstat()
        stamps.append((info.st_dev,info.st_ino,info.st_mtime_ns,info.st_ctime_ns,info.st_size,info.st_mode,info.st_uid))
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or path.resolve() != path):
            raise TargetError('GLOBAL_TARGET_OWNERSHIP_UNKNOWN')
    key = (str(env_file),str(reader))
    with _CACHE_LOCK:
        cached = _SETTINGS.get(key)
        if cached and cached[0] == stamps:
            return dict(cached[1])
    result = subprocess.run(['/bin/bash', '-c',
        '. "$1"; for key in AGENTSTACK_RUNTIME_MODE AGENTSTACK_GLOBAL_SERVER_CONFIG AGENTSTACK_CLIENT_CONFIG; do '
        'agentstack_installed_env_value "$key" "$2" --shell || exit 2; printf "\\n"; done',
        'runtime-target-settings', str(reader), str(env_file)],
        capture_output=True, text=True, timeout=max(.001, min(5, deadline-time.monotonic())))
    if result.returncode:
        raise TargetError('GLOBAL_TARGET_OWNERSHIP_UNKNOWN')
    values = {}
    for line in result.stdout.splitlines():
        if not line:
            continue
        tokens = shlex.split(line)
        if len(tokens) != 2 or tokens[0] != 'export' or '=' not in tokens[1]:
            raise TargetError('GLOBAL_TARGET_OWNERSHIP_UNKNOWN')
        name, value = tokens[1].split('=', 1)
        values[name] = value
    after = []
    for path in (reader,env_file):
        info = path.lstat()
        after.append((info.st_dev,info.st_ino,info.st_mtime_ns,info.st_ctime_ns,info.st_size,info.st_mode,info.st_uid))
    if stamps != after:
        raise TargetError('GLOBAL_TARGET_OWNERSHIP_UNKNOWN')
    with _CACHE_LOCK:
        if len(_SETTINGS) >= 64:
            _SETTINGS.clear()
        _SETTINGS[key] = (stamps,dict(values))
    return values


def global_inventory(client_home, *, api=None, deadline=None):
    """One root reader for admission and repair diagnostics; no new state."""
    if api is None:
        api = runtime_api_view()
    deadline = time.monotonic()+5 if deadline is None else deadline
    home = Path(client_home)
    proofs, diagnostics = [], []
    homes = {home: None}
    def issue(name=None, instance=None, check='placement'):
        diagnostics.append({'client_name': name, 'mail_instance_id': instance,
            'check': check, 'reason': 'GLOBAL_PLACEMENT_UNAVAILABLE' if check=='placement' else 'GLOBAL_ROOT_UNAVAILABLE'})
    def read_failure(exc, name=None, instance=None, check='placement'):
        # Timeout/programming faults are not evidence of a malformed root.
        # Keep the same helper-unavailable classification as main().
        if (isinstance(exc, (KeyError, TypeError, subprocess.SubprocessError))
                or str(exc) in ('OBSERVATION_INCOMPLETE', 'OBSERVATION_BUSY')):
            raise TargetError('TMUX_HELPER_FAILED') from exc
        issue(name, instance, check)
    try:
        installed = installed_hints(home, deadline)
        configs, contexts = set(), set()
        implicit = home/'runtime-client.json'
        if implicit.exists() or implicit.is_symlink():
            contexts.add(implicit)
        for values in (installed, os.environ):
            if values.get('AGENTSTACK_GLOBAL_SERVER_CONFIG'):
                configs.add(api['absolute'](values['AGENTSTACK_GLOBAL_SERVER_CONFIG']))
            if values.get('AGENTSTACK_CLIENT_CONFIG'):
                contexts.add(api['absolute'](values['AGENTSTACK_CLIENT_CONFIG']))
        for path in contexts:
            context = api['read_json'](path)
            if context.get('kind') != api['KIND'] or context.get('mode') != 'global':
                raise TargetError('GLOBAL_TARGET_OWNERSHIP_UNKNOWN')
            runtime = api['absolute'](context.get('runtime_root'))
            configs.add(runtime/'server-config.json')
        for path in configs:
            operator = module_api('runtime_operator')['RuntimeOperator'](path, api=api)
            homes[operator.clients_parent.parent] = operator.binding['expected_server_instance_id']
        if any(values.get('AGENTSTACK_RUNTIME_MODE','legacy') not in ('','legacy')
               for values in (installed,os.environ)) and not configs and not contexts:
            issue()
        for path in contexts:
            context = api['read_json'](path)
            parent = api['absolute'](context.get('agentstack_home'))/'clients'
            if (parent.parent not in homes or path != parent/context.get('client_name','')/api['CLIENT_LAYOUT']['context']):
                raise TargetError('GLOBAL_TARGET_OWNERSHIP_UNKNOWN')
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        read_failure(exc)
    for current, instance in sorted(homes.items()):
        parent = current/'clients'
        try:
            api['remaining'](deadline)
            if not current.exists() and not current.is_symlink():
                if instance is not None: issue(instance=instance)
                continue
            info = current.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022 or current.resolve() != current):
                raise TargetError('GLOBAL_TARGET_OWNERSHIP_UNKNOWN')
            if not parent.exists() and not parent.is_symlink():
                if instance is not None: issue(instance=instance)
                continue
            api['safe_private_directory'](parent)
            roots = sorted(parent.iterdir())
            if not roots and instance is not None: issue(instance=instance)
        except (OSError, ValueError, RuntimeError) as exc:
            read_failure(exc,instance=instance)
            continue
        for root in roots:
            name = root.name if re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', root.name) else None
            check = 'context'
            try:
                api['remaining'](deadline)
                api['validate_client_name'](root.name)
                api['safe_private_directory'](root)
                context = api['read_json'](root/api['CLIENT_LAYOUT']['context'])
                identity = context.get('identity')
                if not isinstance(identity,dict) or not api['integer'](identity.get('agent_id'),1):
                    raise TargetError('GLOBAL_TARGET_OWNERSHIP_UNKNOWN')
                runtime = api['absolute'](context.get('runtime_root'))
                operator = module_api('runtime_operator')['RuntimeOperator'](runtime/'server-config.json',api=api)
                if operator.clients_parent != parent:
                    raise TargetError('GLOBAL_TARGET_OWNERSHIP_UNKNOWN')
                check = 'snapshot'
                snapshot = module_api('runtime_observation')['Observations'](operator,lambda _:None,deadline=deadline)
                value,digest = snapshot.snapshot(root,identity['agent_id'])
                launch = value['launch']
                for live in value['lives']:
                    bound=launch_for_live(live,launch)
                    proofs.append({'live':live,'launch':bound,'digest':digest,'agent_id':identity['agent_id']})
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                read_failure(exc,name,instance,check)
    return {'proofs':proofs,'diagnostics':diagnostics,'global_complete':not diagnostics}


def global_proofs(client_home, *, api=None, deadline=None):
    result = global_inventory(client_home,api=api,deadline=deadline)
    if not result['global_complete']:
        raise TargetError('GLOBAL_TARGET_OWNERSHIP_UNKNOWN')
    return result['proofs']


def legacy_admission(transport, client_home, *, session=None, pane=None, window=None,
                     operation='capture', api=None, proof_reader=None, deadline=None):
    if operation not in OPERATIONS:
        raise TargetError('TMUX_OPERATION_INVALID')
    if api is None:
        api = module_api('runtime_client')
    deadline = time.monotonic() + 5 if deadline is None else deadline
    targets = panes(transport, session=session, pane=pane, window=window, deadline=deadline)
    # A session-wide destructive operation protects every constituent pane.
    if operation == 'kill-session' and session is None:
        targets = panes(transport, session=targets[0]['session'], deadline=deadline)
    reader = global_proofs if proof_reader is None else proof_reader
    proofs = reader(client_home, api=api, deadline=deadline)
    for target in targets:
        start = api['process_start'](target['pid'], deadline=deadline)
        if not start or start == 'absent':
            raise TargetError('TMUX_TARGET_UNAVAILABLE')
        target['process_start'] = start
        for proof in proofs:
            live = proof['live'];launch=launch_for_live(live,proof.get('launch'))
            # The runner still owns its pane after the provider exits. Read its
            # birth from the same launch, without copying a PID into live state.
            runner=(launch or {}).get('execution',{}).get('runner') or live
            if (live.get('pane_id') == target['pane_id'] and pane_process(live,launch) == target['pid']
                    and runner.get('process_start') == start):
                raise TargetError('LEGACY_TARGET_IS_GLOBAL')
    # Re-read socket and actual pane membership after the root snapshots.
    after = panes(transport, session=session, pane=pane, window=window, deadline=deadline)
    if operation == 'kill-session' and session is None:
        after = panes(transport, session=after[0]['session'], deadline=deadline)
    expected = [{k: v for k, v in row.items() if k != 'process_start'} for row in targets]
    if after != expected:
        raise TargetError('TMUX_TARGET_CHANGED')
    for target in targets:
        if api['process_start'](target['pid'], deadline=deadline) != target['process_start']:
            raise TargetError('TMUX_TARGET_CHANGED')
    digest = hashlib.sha256(json.dumps([transport.public(), targets], sort_keys=True).encode()).hexdigest()
    return {'contract': CONTRACT, 'ok': True, 'allowed': True, 'operation': operation,
            **transport.public(), 'panes': targets, 'state_sha256': digest}


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('capabilities')
    sub.add_parser('legacy-transport')
    sub.add_parser('diagnostics')
    admit = sub.add_parser('legacy-target')
    selection = admit.add_mutually_exclusive_group(required=True)
    selection.add_argument('--session')
    selection.add_argument('--pane')
    selection.add_argument('--window')
    admit.add_argument('--operation', choices=OPERATIONS, required=True)
    options = parser.parse_args(argv)
    try:
        if options.command == 'capabilities':
            value = {'contract': CONTRACT, 'capabilities': CAPABILITIES}
        elif options.command == 'diagnostics':
            home = Path(os.environ.get('AGENTSTACK_HOME', str(Path.home()/'.agentstack')))
            inventory = global_inventory(home)
            value = {'contract':CONTRACT,'ok':True,
                     **{k:inventory[k] for k in ('global_complete','diagnostics')}}
        else:
            transport = legacy_transport()
            if options.command == 'legacy-transport':
                value = {'contract': CONTRACT, 'ok': True, **transport.public()}
            else:
                home = Path(os.environ.get('AGENTSTACK_HOME', str(Path.home() / '.agentstack')))
                value = legacy_admission(transport, home, session=options.session,
                                         pane=options.pane, window=options.window, operation=options.operation)
        print_result(value, options.command)
        return 0
    except Exception as exc:
        reason = str(exc) if isinstance(exc,TargetError) and str(exc) in REASONS else 'TMUX_HELPER_FAILED'
        print_result({'contract':CONTRACT,'ok':False,'allowed':False,'reason':reason,
                      'failure_kind':REASONS[reason]},'error')
        return 70 if reason=='TMUX_HELPER_FAILED' else 2




_TARGET_PATH = Path(__file__).with_name('runtime_tmux_target_contract.json')
try:
    TARGET_SPEC = json.loads(_TARGET_PATH.read_text())
except (OSError, ValueError):
    TARGET_SPEC = None
if TARGET_SPEC is None:
    CAPABILITIES = [name for name in CAPABILITIES if not name.startswith('global-target-')]
TARGET_REASONS = {**REASONS, **({key:row['kind'] for key,row in TARGET_SPEC['reasons'].items()} if TARGET_SPEC else {})}

def compact_readiness(text,provider):
    return module_api('terminal_state')['compact_readiness'](text,provider)

def compact_refusal(readiness):
    return module_api('terminal_state')['compact_refusal'](readiness)

def identity_digest(target,principal,proof):
    return module_api('runtime_delivery')['digest'](
        dict(version=1,target=target,principal=principal,proof=proof))


def target_digest(target,principal,proof,readiness,screen=None,provider=None):
    """Compact binds input/context evidence, never unrelated terminal prose."""
    suffix = (module_api('terminal_state')['prompt_suffix'](screen,provider)
              if screen is not None else None)
    return module_api('runtime_delivery')['digest'](dict(
        version=1,identity_sha256=identity_digest(target,principal,proof),
        readiness=readiness,input_suffix=suffix))


def target_result_schema(name):
    if TARGET_SPEC is None:raise TargetError('TMUX_HELPER_FAILED')
    row=TARGET_SPEC['result_tables'][name]
    atoms={**TARGET_SPEC['schema_atoms'],'reason':{'enum':list(TARGET_REASONS)},'failure_kind':{'enum':list(TARGET_SPEC['kind_exit'])}}
    props={key:atoms[row.get('atom_overrides',{}).get(key,key)] for key in row['fields']}
    props.update({key:{'const':value} for key,value in row.get('constants',{}).items()})
    result={'type':'object','properties':props,'required':row['fields'],'additionalProperties':False}
    if name=='error.before_effect':
        return {'anyOf':[{**result,'properties':{**props,'failure_kind':{'const':kind},'reason':{'enum':[key for key,val in TARGET_REASONS.items() if val==kind]}}} for kind in TARGET_SPEC['kind_exit'] if kind!='outcome']}
    return result

def target_reason(exc):
    if TARGET_SPEC is None:return 'TMUX_HELPER_FAILED'
    inputs={source:key for key,row in TARGET_SPEC['reasons'].items() for source in row['reader_inputs']}
    inputs.update({source:key for key,row in TARGET_SPEC['inherited_reader_inputs'].items() for source in row['reader_inputs']})
    if isinstance(exc, (KeyError,TypeError,subprocess.SubprocessError)):return 'TMUX_HELPER_FAILED'
    reason=str(exc)
    return inputs.get(reason,reason if reason in TARGET_REASONS else 'TMUX_HELPER_FAILED')


def LegacyConsumer(*args,**kwargs):
    return module_api('legacy_consumer')['LegacyConsumer'](*args,transport_api=globals(),**kwargs)


def endpoint_candidates(home,instance,launch=None):
    dedicated=global_socket(home,instance)
    if launch and launch.get('tmux_transport')=='global-instance':
        return [('global-instance',Transport(dedicated))]
    values=[]
    environments=[os.environ,{k:v for k,v in os.environ.items() if k not in ('TMUX','TMUX_PANE')}]
    seen=set()
    for environment in environments:
        try:legacy=legacy_transport(environ=environment)
        except TargetError as exc:
            if str(exc)=='GLOBAL_TMUX_LEGACY_CONSUMER_REFUSED':continue
            raise
        if legacy.path not in seen:
            seen.add(legacy.path);values.append(('legacy-default',legacy))
    if launch is None and lstat_if_present(dedicated) is not None:values.append(('global-instance',Transport(dedicated)))
    return values


def launch_for_live(live,launch):
    """An unlaunched standalone slot never inherits another launch's runner."""
    if live.get('launch_id') is None:return None
    if launch is None or any(live.get(k)!=launch.get(k) for k in ('launch_id','receipt_nonce')):
        raise TargetError('TMUX_TARGET_CHANGED')
    return launch


def runner_provider(live,launch,*,api,deadline=None):
    """One birth/UID/ancestry proof; runner transport is never provider liveness."""
    runner=(launch or {}).get('execution',{}).get('runner')
    if runner is None:return api['process_start'](live.get('pid'),deadline=deadline)==live.get('process_start') and bool(live.get('process_start'))
    provider=live.get('pid');pid=runner.get('pid')
    if not all(type(x) is int and x>0 for x in (provider,pid)):return False
    if provider==pid:return False
    def births():
        return (api['process_start'](pid,deadline=deadline)==runner.get('process_start')
            and api['process_start'](provider,deadline=deadline)==live.get('process_start')
            and runner.get('process_start') not in (None,'absent') and live.get('process_start') not in (None,'absent'))
    if not births():return False
    return module_api('process_observation')['belongs_to'](provider,pid,os.getuid(),deadline=deadline) and births()


def pane_process(live,launch=None):
    return (launch or {}).get('execution',{}).get('runner',{}).get('pid',live.get('pid'))


def verify_live_process(live,*,api,deadline=None):
    if api['process_start'](live['pid'],deadline=deadline)!=live['process_start']:
        raise TargetError('TMUX_TARGET_CHANGED')


def verify_live_pane(transport,live,actual,*,api,deadline=None,launch=None):
    """Issue a proof only after all pane/screen observations have completed."""
    rows=panes(transport,pane=actual['pane_id'],deadline=deadline)
    fields=('pid','pane_id','session_id','session')
    if len(rows)!=1 or any(rows[0][key]!=actual[key] for key in fields):
        raise TargetError('TMUX_TARGET_CHANGED')
    if not runner_provider(live,launch,api=api,deadline=deadline):raise TargetError('TMUX_TARGET_CHANGED')


def live_target(live,context,launch=None,*,api=None,deadline=None,endpoints=None):
    """Same explicit socket/pane/PID proof for observation and notifications."""
    api=module_api('runtime_client') if api is None else api
    if live.get('ended'):return None
    launch=launch_for_live(live,launch)
    start=api['process_start'](live.get('pid'),deadline=deadline)
    if not start or start=='absent' or start!=live.get('process_start'):return None
    found=[]
    for kind,transport in (endpoint_candidates(Path(context['agentstack_home']),context['expected_server_instance_id'],launch) if endpoints is None else endpoints):
        try:rows=panes(transport,pane=live.get('pane_id'),deadline=deadline)
        except TargetError as exc:
            if str(exc)=='TMUX_TARGET_UNAVAILABLE':continue
            raise
        for row in rows:
            if row['pid']!=pane_process(live,launch):continue
            if not runner_provider(live,launch,api=api,deadline=deadline):continue
            if launch:
                expected=context['identity']['name'] if kind=='global-instance' else 'orrery-'+launch['launch_id']
                if row['session']!=expected:raise TargetError('TMUX_TARGET_CHANGED')
            verify_live_pane(transport,live,row,api=api,deadline=deadline,launch=launch)
            found.append((transport,row))
    if len(found)>1:raise TargetError('GLOBAL_TARGET_OWNERSHIP_UNKNOWN')
    return found[0] if found else None

if __name__ == '__main__':
    raise SystemExit(main())
