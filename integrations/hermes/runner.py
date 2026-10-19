"""Operator-owned Hermes Mail pilot: one explicit command per lifecycle step.

This is a new harness adapter, not a fallback after another launcher fails.
It invokes the existing product preregistration, config generator and proxy.
The Mail-only pilot is separate from full managed task acceptance.
Configure may resume a saved, matching product MCP projection only before any
profile write. After profile-write-intent exists, retire and use a new attempt.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mail_lifecycle as lifecycle

HERE = Path(__file__).resolve().parent
read, save = lifecycle.read, lifecycle.save
MAIL_TOOLSETS = ('orrery_mail', 'memory', 'skills', 'session_search')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def committed_python(config):
    """Read the selected generation without importing Hermes or running repair."""
    source = Path(config['hermes_source']).resolve()
    key = hashlib.sha256(str(source).encode()).hexdigest()[:16]
    install = Path(config['managed_root']).resolve() / 'installs' / key
    environment = Path(read(install / 'facts.json')['packages']['venv']['environment']).resolve()
    python = environment / 'bin' / 'python'
    if (not environment.is_relative_to((install / 'environments').resolve())
            or not (environment / 'pyvenv.cfg').is_file() or not os.access(python, os.X_OK)):
        raise ValueError('No usable committed dependency interpreter in the managed root')
    return str(python)


def pilot_toolsets(profile_config, requested=None, *, file_task=False):
    """Oneshot accepts configured MCP server names before discovery, not mcp-* names."""
    requested = requested if requested is not None else (*MAIL_TOOLSETS, 'orrery_file') if file_task else MAIL_TOOLSETS
    servers = profile_config.get('mcp_servers', {})
    server = servers.get('orrery_mail', {})
    allowed = {'memory', 'skills', 'session_search'} | set(servers)
    if file_task:
        allowed.add('orrery_file')
    if (not server or server.get('enabled') is False or 'orrery_mail' not in requested
            or any(name not in allowed for name in requested)):
        raise ValueError('Invalid or disabled Mail pilot toolset selection')
    return list(requested)


def base_env(config):
    return {'HOME': config['home'], 'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8',
            'LC_ALL': 'en_US.UTF-8', 'TERM': 'xterm-256color', 'PYTHONDONTWRITEBYTECODE': '1',
            'HERMES_DISABLE_LAZY_INSTALLS': '1', 'UV_OFFLINE': '1'}


def check_profile_paths(profile, config=None):
    profile = Path(profile)
    if profile.is_symlink():
        raise ValueError('Profile may not be a symbolic link')
    root = profile.resolve()
    for path in profile.rglob('*'):
        if path.is_symlink() and not path.resolve().is_relative_to(root):
            raise ValueError('Profile symbolic link points outside the current profile')
    if config is not None:
        skills = config['skills']
        paths = [skills['create_dir'], *skills['external_dirs'], *skills['trusted_project_dirs']]
        if any(not Path(path).is_absolute() or not Path(path).resolve().is_relative_to(root) for path in paths):
            raise ValueError('Skill directories must stay in the current profile')


def initialize(config_path, state, profile_name, run=subprocess.run, *, file_task=False):
    state = Path(state).resolve()
    if state.exists():
        raise ValueError('State already exists; preserve the previous attempt')
    if profile_name not in ('strata', 'deepseek'):
        raise ValueError('Choose the explicit strata or deepseek profile')
    config = read(config_path)
    known = {'product_root', 'home', 'project_key', 'worktree', 'managed_root', 'hermes_executable',
             'hermes_python', 'hermes_source', 'proxy_python', 'tmux', 'parent', 'connection',
             'profiles', 'task_timeout_seconds', 'tmux_socket'}
    profile_keys = {'template', 'provider', 'model', 'base_url', 'api', 'api_mode', 'transport', 'key_cmd',
                    'context_length'}
    if set(config) - known or set(config['connection']) - {'runtime_dir', 'mcp_url', 'mail_env', 'bearer_mode'}:
        raise ValueError('Config may contain only the documented non-secret references')
    if any(set(spec) - profile_keys for spec in config['profiles'].values()):
        raise ValueError('Profile config may contain only route and key-command references')
    for key in ('product_root', 'home', 'project_key', 'worktree', 'managed_root',
                'hermes_executable', 'hermes_python', 'hermes_source', 'proxy_python', 'tmux'):
        if not Path(config[key]).is_absolute():
            raise ValueError('Configured paths must be absolute')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]+', config['parent']):
        raise ValueError('Explicit canonical parent required')
    root = Path(config['managed_root']).resolve()
    home = Path(config['home']).resolve()
    if root == home or root.is_relative_to(home / '.hermes'):
        raise ValueError('Managed root must be separate from the live Hermes home')
    work = Path(config['worktree']).resolve()
    if file_task and (work in (home, Path(work.anchor)) or work.is_relative_to(home / '.hermes')
                      or work.is_relative_to(Path(config['project_key']).resolve())
                      or work.is_relative_to(root / 'profiles')):
        raise ValueError('File trial requires a dedicated worktree outside live data and profiles')
    config['hermes_python'] = committed_python(config)
    profile = root / 'profiles' / profile_name
    Path(config['worktree']).mkdir(parents=True, exist_ok=True)
    state.mkdir(mode=0o700, parents=True)
    spec = config['profiles'][profile_name]
    save(state / 'profile-spec.json', spec, exclusive=True)
    result = run([config['hermes_python'], '-I', '-B', str(HERE / 'profile_projection.py'),
                  '--spec', str(state / 'profile-spec.json'), '--profile', str(profile),
                  '--source', config['hermes_source']], env=base_env(config),
                 capture_output=True, text=True, check=True, timeout=30)
    projection = json.loads(result.stdout)
    config.update(profile_name=profile_name, profile_home=str(profile), managed_root=str(root),
                  model=projection['model'], provider=projection['provider'], purpose='file-task' if file_task else 'mail-connectivity',
                  runner_python=sys.executable, task_timeout_seconds=int(config.get('task_timeout_seconds', 300)))
    save(state / 'run-config.json', config, exclusive=True)
    save(state / 'profile-plan.json', projection, exclusive=True)
    attempt = {'attempt_id': str(uuid.uuid4()), 'phase': 'run', 'agent_name': None,
               'started_at_ns': time.time_ns(), 'purpose': config['purpose']}
    save(state / 'attempt.json', attempt, exclusive=True)
    save(state / 'task.json', {'attempt_id': attempt['attempt_id'], 'task_id': str(uuid.uuid4()),
         'worktree': config['worktree'], 'report_to': config['parent'], 'allowed_paths': [], 'expected': []}, exclusive=True)
    message = {'to': [config['parent']], 'subject': 'Hermes child Mail probe',
               'body_md': 'Hermes child connected through the product bound MCP proxy.'}
    save(state / 'message.json', message, exclusive=True)
    task = ('Your registration is already complete. Do not register, ensure a project, fetch an inbox, '
            'read credentials, or inspect files. Your only task is to call '
            'mcp__orrery_mail__send_message exactly once with this exact JSON:\n' +
            json.dumps(message, ensure_ascii=False) + '\nAfter the tool returns, finish. '
            'If it fails or is unavailable, stop and report failure; do not retry or use another transport.\n')
    (state / 'task.md').write_text(task)
    (state / 'task.md').chmod(0o600)
    if file_task:
        manifest = read(state / 'task.json')
        manifest.update(allowed_paths=['trial-ok.txt'], reservation_receipts=[],
                        reservation_source='product-bound-proxy', expected=[{'path': 'trial-ok.txt', 'contains': 'hermes-first-ok'}])
        target = Path(config['worktree']) / 'trial-ok.txt'
        if not target.exists():
            target.write_text('Initial task artifact. Read before writing.\n')
        message = {'to': [config['parent']], 'subject': 'Hermes reserved file task',
                   'body_md': 'path: ' + str(target) + ' — wrote hermes-first-ok after reading the file.'}
        save(state / 'task.json', manifest)
        save(state / 'message.json', message)
        (state / 'task.md').write_text('Registration is already complete. Do not register, ensure a project, fetch an inbox, '
            'read credentials, or use another transport. Read the absolute path ' + str(target) +
            ' with read_file, then use write_file to write exactly hermes-first-ok followed by a newline. '
            'Then call mcp__orrery_mail__send_message exactly once with this exact JSON:\n' + json.dumps(message) +
            '\nFinish after the tool returns. If any tool fails, stop and report failure; do not retry Mail.\n')
    return {'state': str(state), 'profile': str(profile), 'model': config['model']}


def configure(state, run=subprocess.run):
    state = Path(state).resolve()
    config, identity = read(state / 'run-config.json'), read(state / 'mail-identity.json')
    if (state / 'configured.json').exists():
        raise ValueError('Profile already configured; do not replace a running attempt')
    if any((state / name).exists() for name in ('profile-write-intent.json', 'start-intent.json',
                                               'retire-intent.json', 'retire-receipt.json')):
        raise ValueError('Profile writing, launch or retirement began; retire and use a new attempt')
    profile = Path(config['profile_home'])
    root = Path(config['managed_root']).resolve()
    if profile.resolve() != root / 'profiles' / config['profile_name']:
        raise ValueError('Profile escaped the managed root')
    check_profile_paths(profile)
    marker = profile / '.orrery-profile.json'
    if profile.exists():
        if not marker.is_file():
            raise ValueError('Existing profile is not owned by this adapter')
        prior = read(marker)
        prior_state = Path(prior['state']).resolve()
        if prior_state == state:
            raise ValueError('Profile writing already began; retire and use a new attempt')
        proof = read(prior_state / 'retire-receipt.json')
        prior_attempt = read(prior_state / 'attempt.json')
        if (proof.get('confirmed') is not True or proof.get('agent_name') != prior['agent_name']
                or proof.get('attempt_id') != prior_attempt['attempt_id'] or not proof.get('retired_at')):
            raise ValueError('Previous profile attempt has no parent-confirmed retirement')
    plan = read(state / 'profile-plan.json')['profile_config']
    check_profile_paths(profile, plan)
    # Reuse only a product projection whose complete bound references match.
    # Nothing in the profile has been touched by this attempt at this point.
    if (state / 'mcp.json').exists():
        proxy = lifecycle.generated_proxy(state)
    else:
        proxy = lifecycle.generate(state, run)
    plan['mcp_servers'] = {'orrery_mail': {**proxy, 'tools': {'include': ['send_message']}}}
    pilot_toolsets(plan, file_task=config['purpose'] == 'file-task')
    save(state / 'profile-write-intent.json', {'agent_name': identity['agent_name'],
         'mcp_sha256': digest(state / 'mcp.json'), 'at': lifecycle.stamp()}, exclusive=True)
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ('memories', 'skills', 'sessions', 'logs'):
        (profile / name).mkdir(mode=0o700, exist_ok=True)
    # No other profile's .env, auth.json, memories or skill content is copied.
    save(profile / 'config.yaml', plan)
    plugin = profile / 'plugins' / 'orrery-mail-receipt'
    plugin.mkdir(parents=True, mode=0o700, exist_ok=True)
    save(plugin / 'plugin.yaml', {'name': 'orrery-mail-receipt', 'version': '0.1.0',
         'description': 'Record the product Mail receipt for one explicit parent message',
         'provides_hooks': ['pre_tool_call', 'post_tool_call']})
    (plugin / '__init__.py').write_text(
        'import os, sys\n'
        'sys.path.insert(0, os.environ["ORRERY_HERMES_ADAPTER_DIR"])\n' +
        ('from task_plugin import register\n' if config['purpose'] == 'file-task' else 'from mail_plugin import register\n'))
    (plugin / '__init__.py').chmod(0o600)
    save(marker, {'state': str(state), 'agent_name': identity['agent_name'],
                  'provider': config['provider'], 'model': config['model']})
    proof = {'agent_name': identity['agent_name'], 'profile_home': str(profile),
             'profile_sha256': digest(profile / 'config.yaml'), 'mcp_sha256': digest(state / 'mcp.json'),
             'at': lifecycle.stamp()}
    save(state / 'configured.json', proof, exclusive=True)
    return proof


def check_configured(state):
    state = Path(state)
    config, identity, proof = [read(state / name) for name in
                               ('run-config.json', 'mail-identity.json', 'configured.json')]
    if (proof['agent_name'] != identity['agent_name'] or proof['mcp_sha256'] != digest(state / 'mcp.json')
            or proof['profile_sha256'] != digest(Path(config['profile_home']) / 'config.yaml')):
        raise ValueError('Configured identity or profile changed')
    profile = Path(config['profile_home'])
    if profile.resolve() != Path(config['managed_root']).resolve() / 'profiles' / config['profile_name']:
        raise ValueError('Profile escaped the managed root')
    check_profile_paths(profile, read(profile / 'config.yaml'))
    pilot_toolsets(read(profile / 'config.yaml'), file_task=config['purpose'] == 'file-task')
    config['hermes_python'] = committed_python(config)
    return config, identity


def start(state, run=subprocess.run):
    state = Path(state).resolve()
    config, identity = check_configured(state)
    if any((state / name).exists() for name in ('retire-intent.json', 'retire-receipt.json')):
        raise ValueError('Retirement began; this attempt cannot start')
    probe = read(state / 'mail-probe.json')
    if (probe.get('schema') is not True or probe.get('backend') is not True or probe.get('ok') is not True
            or probe.get('agent_name') != identity['agent_name'] or probe.get('fixture')):
        raise ValueError('Authenticated product Mail preflight is required')
    if config['purpose'] == 'file-task':
        proof = read(state / 'preflight-complete.json')
        if (proof.get('guard_loaded') is not True or proof.get('agent_name') != identity['agent_name']
                or proof.get('attempt_id') != read(state / 'attempt.json')['attempt_id']
                or proof.get('task_sha256') != digest(state / 'task.json')
                or proof.get('profile_sha256') != digest(Path(config['profile_home']) / 'config.yaml')):
            raise ValueError('Current loaded-guard preflight is required')
    if (state / 'start-intent.json').exists():
        raise FileExistsError('A launch was already attempted; preserve it')
    env, tmux = base_env(config), config['tmux']
    socket_path = config.get('tmux_socket')
    if not socket_path:
        result = run([tmux, 'display-message', '-p', '#{socket_path}'], env=env,
                     capture_output=True, text=True, check=True, timeout=10)
        socket_path = result.stdout.strip()
    if not Path(socket_path).is_absolute():
        raise ValueError('An existing canonical tmux socket is required')
    save(state / 'start-intent.json', {'at': lifecycle.stamp(), 'agent_name': identity['agent_name'],
         'socket_path': socket_path}, exclusive=True)
    prefix = [tmux, '-S', socket_path]
    name = identity['agent_name']
    exists = run(prefix + ['has-session', '-t', '=' + name], env=env, capture_output=True, timeout=10)
    if exists.returncode != 1:
        raise ValueError('Child session exists or its absence could not be established')
    command = shlex.join(['/usr/bin/env', '-i', *[key + '=' + value for key, value in env.items()],
                          config['runner_python'], '-B', str(HERE / 'pane_entry.py'), '--state', str(state)])
    Path(config['worktree']).mkdir(parents=True, exist_ok=True)
    result = run(prefix + ['new-session', '-d', '-s', name, '-c', config['worktree'], '-P', '-F', '#{pane_id}', command],
                 env=env, capture_output=True, text=True, check=True, timeout=15)
    pane = result.stdout.strip()
    if not re.fullmatch(r'%\d+', pane):
        raise ValueError('tmux returned no usable pane identity; inspect the same session')
    proof = {'session': name, 'pane_id': pane, 'socket_path': socket_path,
             'agent_name': name, 'at': lifecycle.stamp()}
    save(state / 'owned-pane.json', proof, exclusive=True)
    return proof


def status(state):
    state = Path(state)
    config, identity = read(state / 'run-config.json'), read(state / 'mail-identity.json')
    attempt = read(state / 'attempt.json')
    receipt = read(state / 'mail-send-receipt.json') if (state / 'mail-send-receipt.json').is_file() else None
    accepted = bool(receipt and receipt.get('ok') is True and receipt.get('verified_sender') is True
                    and receipt.get('attempt_id') == attempt['attempt_id']
                    and receipt.get('from') == identity['agent_name']
                    and receipt.get('to') in (config['parent'], [config['parent']])
                    and type(receipt.get('message_id')) is int and receipt['message_id'] > 0)
    runtime = read(state / 'runtime-exit.json') if (state / 'runtime-exit.json').is_file() else None
    result = {'agent_name': identity['agent_name'], 'mail_probe_accepted': accepted,
              'message_id': receipt.get('message_id') if accepted else None,
              'runtime_exit': runtime, 'full_managed_task_accepted': None}
    if config['purpose'] == 'file-task':
        checks = []
        for name in ('task-completion.json', 'runtime-completion.json'):
            value = read(state / name) if (state / name).is_file() else {}
            checks.append(value.get('success') if value.get('attempt_id') == attempt['attempt_id']
                          and type(value.get('success')) is bool else None)
        scan = read(state / 'diagnostic-scan.json') if (state / 'diagnostic-scan.json').is_file() else {}
        current = scan.get('attempt_id') == attempt['attempt_id'] and scan.get('source') == 'operator-private-prefix-scan'
        checks.append(scan['matches'] == 0 and scan.get('errors') == 0
                      if current and type(scan.get('matches')) is int and type(scan.get('errors')) is int else None)
        result['full_managed_task_accepted'] = False if False in checks else None if None in checks else True
    save(state / 'mail-pilot-status.json', result)
    return result


def stop(state, run=subprocess.run):
    state = Path(state)
    config, identity = read(state / 'run-config.json'), read(state / 'mail-identity.json')
    pane = read(state / 'owned-pane.json')
    if pane['session'] != identity['agent_name'] or pane['agent_name'] != identity['agent_name']:
        raise ValueError('Refuse to stop a different identity')
    if not (state / 'runtime-exit.json').is_file():
        run([config['tmux'], '-S', pane['socket_path'], 'kill-session', '-t', '=' + pane['session']],
            env=base_env(config), capture_output=True, check=True, timeout=10)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and not (state / 'runtime-exit.json').is_file():
            time.sleep(0.1)
    runtime = read(state / 'runtime-exit.json')
    if runtime.get('group_absent') is not True or runtime.get('group_check') != 'observed':
        raise ValueError('Owned group is not confirmed absent; ask the operator to inspect')
    return runtime


def managed_env(config, identity, state):
    return base_env(config) | {'HERMES_HOME': config['profile_home'],
        'ORRERY_HERMES_STATE_ROOT': str(Path(state).resolve()), 'ORRERY_HERMES_ADAPTER_DIR': str(HERE),
        'AGENT_NAME': identity['agent_name'], 'PARENT_AGENT': config['parent']}


def apply_reservations(state):
    from mail_probe import normalize_reservations
    state = Path(state)
    attempt, identity, task, receipt, probe = [read(state / name) for name in
        ('attempt.json', 'mail-identity.json', 'task.json', 'reservations.json', 'mail-probe.json')]
    if (receipt.get('source') != 'product-bound-proxy' or receipt['attempt_id'] != attempt['attempt_id']
            or receipt['agent_name'] != identity['agent_name'] or task['attempt_id'] != attempt['attempt_id']
            or probe.get('backend') is not True or probe.get('ok') is not True
            or probe.get('agent_name') != identity['agent_name'] or probe.get('fixture')):
        raise ValueError('Reservations are not bound to this real task')
    rows = normalize_reservations({'granted': receipt['receipts']}, task['allowed_paths'], identity['agent_name'])
    task.update(reservation_source='product-bound-proxy', reservation_receipts=rows)
    save(state / 'task.json', task)
    return receipt


def preflight(state, run=subprocess.run):
    state = Path(state).resolve()
    config, identity = check_configured(state)
    if config['purpose'] != 'file-task':
        raise ValueError('Loaded-guard preflight requires a reserved file task')
    apply_reservations(state)
    (state / 'preflight-complete.json').unlink(missing_ok=True)
    run([config['hermes_python'], '-I', '-B', str(HERE / 'managed_entry.py'), '--state', str(state), '--check'],
        env=managed_env(config, identity, state), cwd=config['worktree'], capture_output=True, check=True, timeout=30)
    return read(state / 'preflight-complete.json')


def evaluate(state, runtime=False):
    import completion
    result = completion.evaluate_runtime(state) if runtime else completion.evaluate_task(state)
    save(Path(state) / ('runtime-completion.json' if runtime else 'task-completion.json'), result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'init-task', 'preregister', 'configure', 'probe', 'reserve', 'release',
        'preflight', 'start', 'status', 'stop', 'retire', 'confirm-retire', 'record-mail', 'complete',
        'complete-runtime', 'scan-diagnostics'])
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--profile', choices=['strata', 'deepseek'])
    parser.add_argument('--input', type=Path, help='Parent-saved canonical whois JSON for confirm-retire')
    args = parser.parse_args()
    exit_code = 0
    try:
        if args.action in ('init', 'init-task'):
            result = initialize(args.config, args.state, args.profile, file_task=args.action == 'init-task')
        elif args.action == 'preregister':
            result = lifecycle.register(args.state, generate_config=False)
        elif args.action == 'configure':
            result = configure(args.state)
        elif args.action in ('probe', 'reserve', 'release'):
            config, _ = check_configured(args.state)
            completed = subprocess.run([config['hermes_python'], '-I', '-B', str(HERE / 'mail_probe.py'),
                 '--state', str(args.state), args.action], env=base_env(config), capture_output=True, text=True, timeout=30)
            result = read(args.state / 'mail-probe.json')
            if completed.returncode or result.get('ok') is not True:
                raise ValueError('Product Mail probe failed; preserve state and stop')
            if args.action == 'reserve':
                result = apply_reservations(args.state)
            elif args.action == 'release':
                result = read(args.state / 'release-receipt.json')
        elif args.action == 'preflight':
            result = preflight(args.state)
        elif args.action == 'start':
            result = start(args.state)
        elif args.action == 'status':
            result = status(args.state)
        elif args.action == 'stop':
            result = stop(args.state)
        elif args.action in ('confirm-retire', 'record-mail'):
            if not args.input:
                raise ValueError('Parent-saved canonical MCP result file required')
            result = lifecycle.confirm_retire(args.state, args.input) if args.action == 'confirm-retire' else lifecycle.record_mail(args.state, args.input)
        elif args.action in ('complete', 'complete-runtime'):
            result = evaluate(args.state, args.action == 'complete-runtime')
            exit_code = 0 if result['success'] is True else 1 if result['success'] is False else 2
        elif args.action == 'scan-diagnostics':
            from diagnostics import scan
            result = scan(args.state)
            exit_code = 0 if result['matches'] == 0 else 1 if result['matches'] else 2
        else:
            config = read(args.state / 'run-config.json')
            task = read(args.state / 'task.json')
            result = lifecycle.retire(args.state, require_release=config['purpose'] == 'file-task'
                                      and bool(task.get('reservation_receipts')))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        raise SystemExit('Step failed; preserve this state and identity, do not retry or switch transports')
    print(json.dumps({'action': args.action, 'result': result}))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
