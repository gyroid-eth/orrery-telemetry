"""Foreground native launch/resume using a prepared fresh owner context.

No registration, trust approval, settings import or background supervisor.
Preparation is manual. An unfinished launch remains unknown until the operator
checks/stops it; absence of a process is never guessed from old names or cwd.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import runpy
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
from uuid import UUID, uuid4

from .fresh_runtime import _directory, _read_private, _write_private
from .fresh_proxy import verified_owner
from .storage_origin_contract import B1Error, digest


def native_id(value):
    try:
        if str(UUID(value)) != value:raise ValueError
        return value
    except (ValueError, TypeError, AttributeError):
        raise B1Error('FRESH_NATIVE_SESSION_INVALID') from None


def layout(client):
    client._load()
    base = client.path.parent / 'launch'
    for folder in (base, base / 'runs', base / 'sessions'):
        folder.mkdir(mode=0o700, exist_ok=True)
        _directory(folder)
    return base


def identity(client):
    return {key: client.context[key] for key in ('mail_instance_id', 'mail_agent_id', 'credential_generation')}


def pinned(client, row):
    if row.get('context_digest') != client.context_digest or any(row.get(k) != v for k, v in identity(client).items()):
        raise B1Error('FRESH_LAUNCH_BINDING_MISMATCH')


@contextmanager
def operation(base):
    fd = os.open(base / 'operation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):raise B1Error('FRESH_LAUNCH_LOCK_UNSAFE')
        try:fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:raise B1Error('FRESH_LAUNCH_BUSY') from None
        yield
    finally:
        os.close(fd)


def root_path(value):
    root = Path(value).resolve(strict=True)
    if not root.is_absolute() or not (root / 'bin/agentstack-runtime-client').is_file():
        raise B1Error('FRESH_WRAPPER_UNAVAILABLE')
    return root


def provider_setup(client, root, provider):
    if provider in {'dsh','hermes'}:
        from .fresh_harness import setup
        return setup(client, root, provider)
    from .fresh_child import provider_home
    home = provider_home(client, provider)
    for folder in (home.parent, home):_directory(folder)
    binary = shutil.which(os.environ.get('AGENTSTACK_' + provider.upper() + '_BIN', provider))
    if binary is None:raise B1Error('FRESH_PROVIDER_UNAVAILABLE')
    instructions = runpy.run_path(str(root / 'bin/lib/instruction_blocks.py'))
    try:instructions['provider_instructions'](home, provider, root, '', template_home=root / 'templates/fresh')
    except (OSError, ValueError, instructions['Refused']):
        raise B1Error('INSTRUCTION_PROVIDER_CONFIG_REQUIRED') from None
    plan = None
    if provider == 'codex':
        plan = _read_private(home / 'hook-plan.json')
        trust = runpy.run_path(str(root / 'scripts/codex_plugin_trust.py'))
        try:
            if (plan.get('shared_codex_home') != str(home) or plan.get('cwd') != client.context['root']
                    or plan.get('codex_binary') != binary):raise ValueError
            trust['verify_approved_plan'](plan)
            # A stale installed hook payload cannot silently keep the old route.
            if ((Path(plan['plugin_root']) / 'scripts/run-hook.sh').read_bytes()
                    != (root / 'integrations/codex_app/plugin/scripts/run-hook.sh').read_bytes()
                    or (Path(plan['plugin_root']) / 'scripts/run-reservation-guard.sh').read_bytes()
                    != (root / 'integrations/codex_app/plugin/scripts/run-reservation-guard.sh').read_bytes()
                    or (Path(plan['plugin_root']) / 'hooks/hooks.json').read_bytes()
                    != (root / 'integrations/codex_app/plugin/hooks/hooks.json').read_bytes()):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError, trust['TrustUnavailable']):
            raise B1Error('CODEX_HOOK_CONFIGURE_REQUIRED') from None
    return home, binary, plan


def command(root, home, binary, provider, plan, run, cwd, session_id):
    proxy = ['/bin/bash', str(root / 'bin/agentstack-runtime-client'), 'proxy']
    policy = runpy.run_path(str(root / 'hooks/child_tools.py'))
    if provider == 'claude':
        saved = policy['_load_claude_settings'](str(home / '.claude.json'))
        extra = {}
        for scope in (saved.get('mcpServers', {}), policy['_project_entry'](saved, str(cwd)).get('mcpServers', {})):
            for name in scope:
                if not policy['looks_like_agent_mail'](name):
                    extra[name] = policy['_copyable_definition'](name, saved, str(cwd))
        config = run / 'mcp.json'
        _write_private(config, {'mcpServers': {**extra, 'orrery-mail': {'command': proxy[0], 'args': proxy[1:]}}})
        session_hook = shlex.join(['/bin/bash', str(root / 'bin/agentstack-runtime-client'), 'notifications'])
        guard = shlex.join(['/bin/bash', str(root / 'hooks/check-file-reservation.sh')])
        settings = run / 'hooks.json'
        _write_private(settings, {'hooks': {
            'SessionStart': [{'hooks': [{'type': 'command', 'command': session_hook}]}],
            'PostToolUse': [{'hooks': [{'type': 'command', 'command': session_hook}]}],
            'PreToolUse': [{'matcher': 'Edit|Write|MultiEdit|NotebookEdit|Delete|MoveSource',
                           'hooks': [{'type': 'command', 'command': guard}]}]}})
        args = [binary, '--mcp-config', str(config), '--strict-mcp-config', '--settings', str(settings)]
        if session_id is not None:args += ['--resume', session_id]
        return args
    config = home / 'config.toml'  # verify_approved_plan already required private native config.
    import tomllib
    values = tomllib.loads(config.read_text())
    args = [binary, '-C', str(cwd), '-c', 'check_for_update_on_startup=false',
            '--sandbox', os.environ.get('AGENTSTACK_CODEX_SANDBOX', 'workspace-write'),
            '--ask-for-approval', os.environ.get('AGENTSTACK_CODEX_APPROVAL', 'on-request'),
            '-c', 'mcp_servers.orrery-mail.command=' + json.dumps(proxy[0]),
            '-c', 'mcp_servers.orrery-mail.args=' + json.dumps(proxy[1:]),
            '-c', 'plugins.' + json.dumps(plan['plugin_id']) + '.mcp_servers.agentstack.enabled=false']
    for name in values.get('mcp_servers', {}):
        if name != 'orrery-mail' and policy['looks_like_agent_mail'](name):
            args += ['-c', 'mcp_servers.' + json.dumps(name) + '.enabled=false']
    vault = os.environ.get('AGENTSTACK_VAULT')
    if vault and Path(vault).is_dir():args += ['--add-dir', vault]
    if session_id is not None:args += ['resume', session_id]
    return args


def launch(client, wrapper_root, provider, cwd, *, session_id=None):
    root = root_path(wrapper_root)
    cwd = Path(cwd).resolve(strict=True)
    if not cwd.is_dir() or provider not in {'claude', 'codex', 'dsh', 'hermes'}:raise B1Error('FRESH_LAUNCH_ARGUMENTS_INVALID')
    managed = provider in {'dsh','hermes'}
    owner = asyncio.run(verified_owner(client))
    from .fresh_child import family, launch_task
    task, prompt = launch_task(client)
    if task is not None and (task['provider'] != provider or task['cwd'] != str(cwd) or task['wrapper_root'] != str(root)):
        raise B1Error('FRESH_CHILD_INTENT_CONFLICT')
    base = layout(client)
    with operation(base):
        current = base / 'current.json'
        if current.exists() or current.is_symlink():
            previous = _read_private(current)
            previous_run = base / 'runs' / native_id(previous.get('launch_id'))
            _directory(previous_run)
            if not (previous_run / 'ended.json').exists():raise B1Error('FRESH_LAUNCH_OUTCOME_UNKNOWN')
            ended = _read_private(previous_run / 'ended.json')
            if (set(ended) != {'launch_id', 'exit_code'} or ended['launch_id'] != previous['launch_id']
                    or type(ended['exit_code']) is not int):raise B1Error('FRESH_LAUNCH_OUTCOME_UNKNOWN')
        home, binary, plan = provider_setup(client, root, provider)
        if session_id is not None:
            if managed:
                from .fresh_harness import native_session_id
                session_id = native_session_id(session_id)
            else:session_id = native_id(session_id)
            receipt = _read_private(base / 'sessions' / (session_id + '.json'))
            pinned(client, receipt)
            if receipt.get('provider') != provider or receipt.get('native_session_id') != session_id:
                raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
            transcript = Path(receipt['transcript_path'])
            expected_root = client.path.parent if managed else home
            if not transcript.resolve(strict=True).is_relative_to(expected_root):raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
            if managed:
                previous_native = _read_private(transcript)
                if previous_native.get('harness') != provider or previous_native.get('native_session_id') != session_id:
                    raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
            if provider == 'codex':
                reader = runpy.run_path(str(root / 'hooks/prepare-codex-session-binding.py'))
                if reader['_transcript_header_session_id'](transcript) != session_id:
                    raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
        launch_id = str(uuid4())
        run = base / 'runs' / launch_id
        run.mkdir(mode=0o700);_directory(run)
        intent = dict(kind='orrery-fresh-launch-v1', launch_id=launch_id, provider=provider, cwd=str(cwd),
                      context_digest=client.context_digest, wrapper_root=str(root),
                      resume_session_id=session_id, nonce=secrets.token_hex(32), **identity(client))
        managed_plan = None
        if managed:
            from .fresh_harness import plan as native_plan
            managed_plan = native_plan(client, root, provider, plan, run, cwd,
                                       prompt if session_id is None else '', session_id)
            args = managed_plan['argv']
        else:args = command(root, home, binary, provider, plan, run, cwd, session_id)
        if task is not None:
            _, _, child_record = family(client)
            if binary != task['binary']:raise B1Error('FRESH_CHILD_INTENT_CONFLICT')
            if not managed:
                args += ['--model', child_record['model']]
                effort = child_record.get('effort')
                if effort:
                    args += (['--effort', effort] if provider == 'claude' else
                             ['-c', 'model_reasoning_effort=' + json.dumps(effort)])
                args += ['--', prompt]
            intent['child_spawn_id'] = task['spawn_id']
        from .fresh_delivery import capture, publish
        captured = capture(client, root, launch_id)
        _write_private(run / 'intent.json', intent)
        # A pointer is only an index: previous immutable runs/receipts stay put.
        temporary = base / ('.current-' + launch_id)
        _write_private(temporary, {'launch_id': launch_id})
        os.replace(temporary, current)
        environment = dict(os.environ)
        for key in list(environment):
            if (key in {'AGENTSTACK_PROJECT_KEY', 'PROJECT_KEY', 'PARENT_AGENT', 'CHILD_REGISTRATION_TOKEN',
                        'AGENTSTACK_RESERVED_IDENTITY', 'MCP_URL', 'MCP_AGENT_MAIL_TOKEN', 'OPENAI_API_KEY',
                        'AGENTSTACK_CLIENT_CONFIG', 'AGENTSTACK_GLOBAL_SERVER_CONFIG', 'AGENTSTACK_MCP_URL',
                        'AGENTSTACK_MAIL_ENV', 'AGENTSTACK_MANAGED_AGENTS_FILE',
                        'AGENTSTACK_CODEX_LAUNCH_BINDING', 'AGENTSTACK_CODEX_LAUNCH_ID', 'AGENTSTACK_CODEX_LAUNCH_KIND'}
                    or key.startswith(('AGENTSTACK_LOOKUP_', 'AGENTSTACK_PROXY_'))):environment.pop(key)
        environment.update(AGENTSTACK_RUNTIME_MODE='fresh', AGENTSTACK_FRESH_CLIENT_CONTEXT=str(client.path),
            AGENTSTACK_FRESH_LAUNCH_ID=launch_id, AGENTSTACK_FRESH_LAUNCH_NONCE=intent['nonce'],
            AGENTSTACK_HOME=str(root), AGENTSTACK_PYTHON=sys.executable, AGENT_NAME=owner['agent_name'],
            AGENTSTACK_AGENT_ID=str(owner['mail_agent_id']))
        runtime = base / 'runtime'
        runtime.mkdir(mode=0o700, exist_ok=True);_directory(runtime)
        environment.update(AGENTSTACK_RUNTIME_DIR=str(runtime), AGENTSTACK_HOOKS_DIR=str(root / 'hooks'))
        if managed:environment.update(managed_plan['env'])
        else:environment['CODEX_HOME' if provider == 'codex' else 'CLAUDE_CONFIG_DIR'] = str(home)
        process = None
        try:
            process = subprocess.Popen(args, cwd=cwd, env=environment)
            publish(run, captured, process, root)
            code = process.wait()
        except BaseException:
            if process is not None and process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            # Leave current as unknown; recovery is a documented operator action.
            raise
        _write_private(run / 'ended.json', {'launch_id': launch_id, 'exit_code': code})
        if code == 0:
            observed = _read_private(run / 'session.json')
            pinned(client, observed)
        return code


def session_state(client, payload):
    """Re-read the launch gates for each native callback/publication."""
    base = layout(client)
    launch_id = native_id(os.environ.get('AGENTSTACK_FRESH_LAUNCH_ID'))
    run = base / 'runs' / launch_id
    _directory(run)
    intent = _read_private(run / 'intent.json')
    pinned(client, intent)
    if (intent.get('launch_id') != launch_id or intent.get('nonce') != os.environ.get('AGENTSTACK_FRESH_LAUNCH_NONCE')
            or (run / 'ended.json').exists()):raise B1Error('FRESH_LAUNCH_RECEIPT_MISMATCH')
    current = _read_private(base / 'current.json')
    if current != {'launch_id': launch_id}:raise B1Error('FRESH_LAUNCH_RECEIPT_MISMATCH')
    managed = intent['provider'] in {'dsh','hermes'}
    if managed:
        from .fresh_harness import native_session_id
        session_id = native_session_id(payload.get('session_id'))
    else:session_id = native_id(payload.get('session_id'))
    if intent['resume_session_id'] is not None and intent['resume_session_id'] != session_id:
        raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
    return base, run, intent, session_id


def same_session_file(path, value):
    try:_write_private(path, value)
    except FileExistsError:
        if _read_private(path) != value:raise B1Error('FRESH_NATIVE_SESSION_MISMATCH') from None


def session_candidate(client, run, intent, session_id, transcript, home):
    # Non-strict resolution admits a candidate only; it never proves readiness.
    resolved = transcript.resolve()
    if not transcript.is_absolute() or not resolved.is_relative_to(home):
        raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
    return dict(kind='orrery-fresh-session-start-v1', launch_id=run.name,
        provider='claude', context_digest=client.context_digest, **identity(client),
        native_session_id=session_id, transcript_path=str(transcript),
        resolved_transcript_path=str(resolved), provider_home=str(home), intent_digest=digest(intent))


def publish_session(client, payload, state, transcript):
    base, run, intent, session_id = state
    checked = session_state(client, payload)
    if checked != state:raise B1Error('FRESH_LAUNCH_RECEIPT_MISMATCH')
    receipt = dict(kind='orrery-fresh-native-session-v1', context_digest=client.context_digest,
                   provider=intent['provider'], native_session_id=session_id,
                   transcript_path=str(transcript), **identity(client))
    for path, value in ((base / 'sessions' / (session_id + '.json'), receipt),
                        (run / 'session.json', dict(receipt, launch_id=run.name))):
        same_session_file(path, value)


async def record_session(client, payload, *, defer_transcript=False):
    if not isinstance(payload, dict):raise B1Error('FRESH_SESSION_EVENT_INVALID')
    if payload.get('hook_event_name') != 'SessionStart':return
    await verified_owner(client)
    state = session_state(client, payload)
    base, run, intent, session_id = state
    managed = intent['provider'] in {'dsh','hermes'}
    from .fresh_child import provider_home
    if managed:
        from .fresh_harness import session_reference
        transcript = session_reference(client, intent, session_id)
    else:
        home = provider_home(client, intent['provider'])
        transcript = Path(payload.get('transcript_path', ''))
        if not transcript.is_absolute():raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
        candidate = None
        path = run / 'session-start.json'
        if intent['provider'] == 'claude' and (defer_transcript or path.exists() or path.is_symlink()):
            candidate = session_candidate(client, run, intent, session_id, transcript, home)
            if (path.exists() or path.is_symlink()) and _read_private(path) != candidate:
                raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
        try:resolved = transcript.resolve(strict=True)
        except FileNotFoundError:
            if (not defer_transcript or candidate is None or (run / 'session.json').exists()
                    or (run / 'session.json').is_symlink()):raise
            if session_state(client, payload) != state:raise B1Error('FRESH_LAUNCH_RECEIPT_MISMATCH')
            same_session_file(path, candidate)
            return False
        if (not resolved.is_relative_to(home)
                or (candidate is not None and str(resolved) != candidate['resolved_transcript_path'])):
            raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
    if intent['provider'] == 'codex':
        root = root_path(intent['wrapper_root'])
        reader = runpy.run_path(str(root / 'hooks/prepare-codex-session-binding.py'))
        if reader['_transcript_header_session_id'](transcript) != session_id:
            raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
    publish_session(client, payload, state, transcript.resolve() if managed else resolved)
    return True


async def complete_pending_session(client, payload):
    """A PostToolUse can complete only an admitted Claude SessionStart."""
    base = layout(client)
    run = base / 'runs' / native_id(os.environ.get('AGENTSTACK_FRESH_LAUNCH_ID'))
    _directory(run)
    if (run / 'session.json').exists() or (run / 'session.json').is_symlink():return True
    await verified_owner(client)
    state = session_state(client, payload)
    base, run, intent, session_id = state
    if intent['provider'] != 'claude':raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
    saved = _read_private(run / 'session-start.json')
    if not isinstance(saved.get('transcript_path'), str):raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
    from .fresh_child import provider_home
    home = provider_home(client, 'claude')
    transcript = Path(saved['transcript_path'])
    if ('transcript_path' in payload and payload['transcript_path'] != str(transcript)):
        raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
    if saved != session_candidate(client, run, intent, session_id, transcript, home):
        raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
    try:resolved = transcript.resolve(strict=True)
    except FileNotFoundError:return False
    if not resolved.is_relative_to(home) or str(resolved) != saved['resolved_transcript_path']:
        raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
    publish_session(client, payload, state, resolved)
    return True
