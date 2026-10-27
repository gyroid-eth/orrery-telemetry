"""Trusted managed-native glue; selection/argv stay in the existing generators."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import runpy
import sys
import types

from .fresh_runtime import _directory, _read_private, _write_private
from .storage_origin_contract import B1Error

PROGRAMS = {'dsh', 'hermes'}


def native_session_id(value):
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value) is None:
        raise B1Error('FRESH_NATIVE_SESSION_INVALID')
    return value


def helper(root):
    return runpy.run_path(str(Path(root) / 'hooks/harness_child.py'))


def selection(root, program, model, spec):
    if program not in PROGRAMS or not isinstance(spec, dict) or set(spec) - {'manifest_file','backend_id','effort','helper_model','tools','url_index','url_key','server_url'}:
        raise B1Error('FRESH_HARNESS_SELECTION_INVALID')
    try:
        manifest = Path(spec['manifest_file'])
        if not manifest.is_absolute() or manifest.resolve(strict=True) != manifest:raise ValueError
        api = helper(root)
        if 'url_index' in spec and 'url_key' not in spec:raise ValueError
        runner = api['selection'](str(manifest), program, spec['backend_id'], model, spec.get('effort'),
                                  spec.get('url_index'), spec.get('server_url'), spec.get('url_key'))
        template = api['read'](runner['config_file'])
        binary = str(Path(template['node' if program == 'dsh' else 'hermes_executable']).resolve(strict=True))
        files = [manifest, Path(runner['config_file']), Path(template['generation_receipt'])]
        pins = {str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        return runner, binary, pins
    except (ValueError, KeyError, TypeError, OSError):
        raise B1Error('FRESH_HARNESS_CONFIGURE_REQUIRED') from None


def setup(client, root, provider):
    from .fresh_child import family
    _, _, ready = family(client)
    if ready is None or ready['program'] != provider or not ready.get('managed'):
        raise B1Error('FRESH_HARNESS_SELECTION_REQUIRED')
    spec = ready['managed']
    runner, binary, pins = selection(root, provider, ready['model'], spec['selection'])
    if pins != spec['pins']:raise B1Error('FRESH_HARNESS_SELECTION_CHANGED')
    return client.path.parent / 'providers' / provider, binary, dict(runner=runner, spec=spec, model=ready['model'])


def plan(client, root, provider, settings, run, cwd, prompt, session_id):
    api = helper(root)
    for folder in (client.path.parent / 'providers', client.path.parent / 'providers' / provider):
        folder.mkdir(mode=0o700, exist_ok=True);_directory(folder)
    state = run / 'managed'
    state.mkdir(mode=0o700);_directory(state)
    instructions = api['fresh_instructions'](state, root)
    task = state / 'prompt.txt'
    fd = os.open(task, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:output.write(prompt or '')
    mcp = state / 'mail.json'
    env = dict(AGENTSTACK_RUNTIME_MODE='fresh', AGENTSTACK_FRESH_CLIENT_CONTEXT=str(client.path))
    _write_private(mcp, {'mcpServers': {'orrery-mail': {'command':'/bin/bash',
        'args':[str(root / 'bin/agentstack-runtime-client'),'proxy'], 'env':env}}})
    from .fresh_proxy import verified_owner
    owner = asyncio.run(verified_owner(client))
    selection_spec = settings['spec']['selection']
    document = dict(program=provider, agent_name=owner['agent_name'], model=settings['model'],
        manifest_file=selection_spec['manifest_file'], backend_id=selection_spec['backend_id'],
        backend_kind=settings['runner']['connection_kind'], product_root=str(root),
        state_dir=str(state), runtime_dir=str(client.path.parent), fresh_client_dir=str(client.path.parent),
        cwd=str(cwd), mcp_config=str(mcp), prompt_file=str(task), instructions_file=str(instructions),
        tools=selection_spec.get('tools', {'base':'default','tools':{}}), env=env)
    for key in ('effort','helper_model'):
        if key in selection_spec:document[key] = selection_spec[key]
    document.update({key:settings['runner'][key] for key in ('url_index','url_key','server_url') if key in settings['runner']})
    if session_id is not None:
        home = client.path.parent / 'providers' / provider
        document.update(resume_session_id=session_id, native_profile_env={
            'DSH_HOME' if provider == 'dsh' else 'HERMES_HOME':str(home)})
    try:
        runner = api['dynamic_prelaunch'](document)
        api['generate_plan'](document, runner if runner is not None else settings['runner'])
        result = api['read'](state / 'launch.json')
        api['validate_plan'](result, str(cwd))
    except (ValueError, OSError, KeyError, TypeError):
        raise B1Error('FRESH_HARNESS_CONFIGURE_REQUIRED') from None
    result['env'].update(AGENTSTACK_NATIVE_SESSION_FILE=str(state / 'native-session.json'))
    return result


def readiness(root, text, provider):
    """Observe native activity AND an empty current composer; no I/O inference."""
    package_name = '_orrery_fresh_activity_' + hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(Path(root) / 'dashboard')]
        sys.modules[package_name] = package
    api = importlib.import_module(package_name + '.harness_activity')
    activity = api.parse_activity(text, provider)
    lines = api._ANSI.sub('', text).replace('\r', '').splitlines()[-45:]
    empty = False
    if provider == 'hermes':
        prompt = api._hermes_prompt(lines)
        empty = prompt is not None and re.fullmatch(r'(?:(?:[\w.-]+\s+)?❯|☤(?:\s+❯)?)', prompt) is not None
    elif provider == 'dsh':
        rows = [line.strip() for line in lines if line.strip()]
        # Match the same last current frame observed by the native parser.
        for index in reversed(range(max(0, len(rows) - 10), len(rows) - 2)):
            top, prompt, bottom = rows[index:index+3]
            if (re.fullmatch(r'╭─{8,}╮', top) and bottom == '╰' + top[1:-1] + '╯'
                    and re.fullmatch(r'⌸\s+❯.*\s+⛶', prompt)):
                empty = re.fullmatch(r'⌸\s+❯\s+⛶', prompt) is not None
                break
    return dict(state={'wait':'idle','work':'work'}.get(activity,'unknown'), input_empty=empty)


def session_reference(client, intent, session_id):
    """The native plugin wrote this launch's private sidecar before the hook."""
    path = Path(os.environ.get('AGENTSTACK_NATIVE_SESSION_FILE', ''))
    run = client.path.parent / 'launch/runs' / intent['launch_id']
    if path != run / 'managed/native-session.json':raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
    value = _read_private(path)
    if value.get('harness') != intent['provider'] or value.get('native_session_id') != session_id:
        raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
    return path


async def hook(client, action, payload):
    from .fresh_launch import layout, native_id, pinned, record_session
    from .fresh_proxy import verified_owner
    if (os.environ.get('AGENTSTACK_FRESH_CLIENT_CONTEXT') != str(client.path)
            or os.environ.get('AGENTSTACK_RUNTIME_MODE','legacy') not in ('legacy','fresh')
            or os.environ.get('AGENTSTACK_CLIENT_CONFIG') or os.environ.get('AGENTSTACK_GLOBAL_SERVER_CONFIG')):
        raise B1Error('FRESH_CONTEXT_REQUIRED')
    if not isinstance(payload, dict):raise B1Error('FRESH_HOOK_EVENT_INVALID')
    if action == 'registered' and payload.get('hook_event_name') == 'SessionStart':
        await record_session(client, payload)
    await verified_owner(client)
    base = layout(client)
    launch_id = native_id(os.environ.get('AGENTSTACK_FRESH_LAUNCH_ID'))
    run = base / 'runs' / launch_id
    intent = _read_private(run / 'intent.json');pinned(client, intent)
    receipt = _read_private(run / 'session.json');pinned(client, receipt)
    if (intent.get('provider') not in PROGRAMS | {'claude','codex'} or intent.get('nonce') != os.environ.get('AGENTSTACK_FRESH_LAUNCH_NONCE')
            or receipt.get('launch_id') != launch_id or receipt.get('provider') != intent['provider']
            or receipt.get('native_session_id') != payload.get('session_id')
            or _read_private(base / 'current.json') != {'launch_id':launch_id}
            or (run / 'ended.json').exists()):raise B1Error('FRESH_LAUNCH_RECEIPT_MISMATCH')
    if action == 'post-edit':
        # Fresh leases are explicit release/TTL, as for the existing fresh clients.
        # Do not dispatch the legacy project-scoped debounce worker after an edit.
        return
    if action != 'registered':raise B1Error('FRESH_HOOK_EVENT_INVALID')

