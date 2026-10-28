"""Trusted local child preparation and the existing tmux/native launch handoff.

This module is a host CLI, never a model proxy tool. Owner credentials stay in
the existing bindings. Child records contain references, IDs and task bytes.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import re
import runpy
import shlex
import sys
from uuid import uuid4

from .fresh_client import FreshBoundClient
from .fresh_launch import identity, native_id, operation, pinned, provider_setup, root_path
from .fresh_proxy import verified_owner
from .fresh_runtime import FreshRuntime, _directory, _read_private, _write_private
from .storage_origin_contract import B1Error, canonical


def slot_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', value):
        raise B1Error('FRESH_CHILD_SLOT_INVALID')
    return value


def children(parent, slot):
    base = parent.path.parent / 'children'
    for folder in (base, base / slot_name(slot)):
        folder.mkdir(mode=0o700, exist_ok=True);_directory(folder)
    return base / slot


def same(path, value):
    try:_write_private(path, value)
    except FileExistsError:
        if _read_private(path) != value:raise B1Error('FRESH_CHILD_INTENT_CONFLICT') from None


def read_task(base):
    # Explicit task record only; other private metadata retains 64 KiB.
    return _read_private(base / 'task.json', max_bytes=1024 * 1024)


async def prepare(parent, slot, name, program, model, description, *, managed=None, standalone=False, effort=None):
    await verified_owner(parent)
    slot = slot_name(slot)
    if program not in ('claude-code', 'codex', 'dsh', 'hermes') or not all(isinstance(v, str) and v for v in (name, model, description)):
        raise B1Error('FRESH_CHILD_ARGUMENTS_INVALID')
    if len(name) > 64 or len(model) > 128 or len(description.encode()) > 8192:
        raise B1Error('FRESH_CHILD_ARGUMENTS_INVALID')
    if type(standalone) is not bool or (effort is not None and
            (program not in ('claude-code','codex') or effort not in ('low','medium','high','xhigh','max','ultra'))):
        raise B1Error('FRESH_CHILD_ARGUMENTS_INVALID')
    base = children(parent, slot)
    with operation(base):
        path = base / 'registration.json'
        values = dict(name=name, program=program, model=model, description=description,
                      parent_context=str(parent.path), parent_digest=parent.context_digest, **identity(parent))
        if standalone:values['standalone'] = True
        if effort:values['effort'] = effort
        if program in {'dsh','hermes'}:
            if not isinstance(managed, dict):raise B1Error('FRESH_HARNESS_SELECTION_REQUIRED')
            values['managed'] = managed
        elif managed is not None:raise B1Error('FRESH_CHILD_ARGUMENTS_INVALID')
        if not path.exists():
            _write_private(path, dict(values, registration_id=str(uuid4()), client_id=str(uuid4())))
        saved = _read_private(path)
        if {key:saved.get(key) for key in values} != values:
            raise B1Error('FRESH_CHILD_INTENT_CONFLICT')
        runtime = await FreshRuntime.open(Path(parent.context['root']) / 'config.json')
        try:
            if runtime.config['instance_id'] != parent.context['mail_instance_id']:
                raise B1Error('FRESH_CLIENT_CONFIG_MISMATCH')
            result = await runtime.register_local(request_id=saved['registration_id'], name=name,
                                                 program=program, model=model)
        finally:await runtime.close()
        child = await FreshBoundClient.prepare(Path(parent.context['root']) / 'config.json', result['binding_path'],
                                              parent.context['endpoint'], client_id=saved['client_id'])
        if child.context['mail_agent_id'] == parent.context['mail_agent_id']:
            raise B1Error('FRESH_CHILD_OWNER_MISMATCH')
        ready = dict(kind='orrery-fresh-child-v1', slot=slot, parent_context=str(parent.path),
                     parent_digest=parent.context_digest, parent_identity=identity(parent),
                     child_context=str(child.path), child_digest=child.context_digest,
                     child_identity=identity(child), name=name, program=program, model=model)
        if managed is not None:ready['managed'] = managed
        if standalone:ready['standalone'] = True
        if effort:ready['effort'] = effort
        same(base / 'prepared.json', ready)
        same(child.path.parent / 'child.json', dict(parent_context=str(parent.path), slot=slot))
        return dict(prepared=True, running=False, name=name, child_client_name=slot,
                    context_path=str(child.path), **identity(child))


def family(client):
    """Follow only protected prepared lineage, never names or arbitrary HOME."""
    path = client.path.parent / 'child.json'
    if not path.exists() and not path.is_symlink():return client, None, None
    link = _read_private(path)
    if set(link) != {'parent_context', 'slot'}:raise B1Error('FRESH_CHILD_BINDING_MISMATCH')
    parent = FreshBoundClient(link['parent_context'])
    base = children(parent, slot_name(link['slot']))
    ready = _read_private(base / 'prepared.json')
    if (ready.get('kind') != 'orrery-fresh-child-v1' or ready.get('slot') != link['slot']
            or ready.get('child_context') != str(client.path) or ready.get('child_digest') != client.context_digest
            or ready.get('child_identity') != identity(client) or ready.get('parent_context') != str(parent.path)
            or ready.get('parent_digest') != parent.context_digest or ready.get('parent_identity') != identity(parent)
            or client.context['mail_instance_id'] != parent.context['mail_instance_id']
            or client.context['root'] != parent.context['root']
            or client.context['mail_agent_id'] == parent.context['mail_agent_id']):
        raise B1Error('FRESH_CHILD_BINDING_MISMATCH')
    return parent, base, ready


def provider_home(client, provider):
    seen = set()
    for _ in range(32):
        if str(client.path) in seen:raise B1Error('FRESH_CHILD_BINDING_MISMATCH')
        seen.add(str(client.path))
        parent, base, ready = family(client)
        if base is None:return client.path.parent / 'providers' / provider
        client = parent
    raise B1Error('FRESH_CHILD_BINDING_MISMATCH')


async def selected_child(parent, slot, name):
    await verified_owner(parent)
    if slot is None:
        directory = parent.path.parent / 'children';_directory(directory)
        matches = []
        for item in directory.iterdir():
            _directory(item)
            path = item / 'prepared.json'
            if path.exists() and _read_private(path).get('name') == name:matches.append(item.name)
        if len(matches) != 1:raise B1Error('FRESH_CHILD_SELECTOR_AMBIGUOUS')
        slot = matches[0]
    base = children(parent, slot)
    ready = _read_private(base / 'prepared.json')
    if name is not None and ready['name'] != name:raise B1Error('FRESH_CHILD_BINDING_MISMATCH')
    child = FreshBoundClient(ready['child_context'])
    verified_parent, checked, _ = family(child)
    if verified_parent.path != parent.path or checked != base:raise B1Error('FRESH_CHILD_BINDING_MISMATCH')
    await verified_owner(child)
    return base, ready, child


def task_text(path):
    # Task files are ordinary user files, not private credential/binding files.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        import stat
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():raise B1Error('FRESH_CHILD_TASK_INVALID')
        with os.fdopen(fd, 'rb', closefd=False) as stream:raw = stream.read(65537)
        if not raw or len(raw) > 65536:raise B1Error('FRESH_CHILD_TASK_INVALID')
        return raw.decode('utf-8')
    finally:os.close(fd)


async def handoff(parent, root, *, slot=None, name=None, cwd, task, resources, ttl=14400,
                  unsafe_no_resources=False, prepare_only=False, transport=None):
    base, ready, child = await selected_child(parent, slot, name)
    provider = {'claude-code':'claude'}.get(ready['program'],ready['program'])
    cwd = Path(cwd).resolve(strict=True)
    if not cwd.is_dir() or type(ttl) is not int or ttl < 900:
        raise B1Error('FRESH_CHILD_ARGUMENTS_INVALID')
    if bool(resources) == bool(unsafe_no_resources):raise B1Error('FRESH_CHILD_RESOURCES_REQUIRED')
    paths = [os.path.abspath(cwd / path) for path in resources]
    if not isinstance(task, str) or not task or len(task.encode()) > 65536:
        raise B1Error('FRESH_CHILD_TASK_INVALID')
    # Reuse preparation as a read-only gate. In particular no hook approval.
    _, binary, _ = provider_setup(child, root, provider)
    with operation(base):
        values = dict(cwd=str(cwd), task=task, resources=paths, ttl=ttl, wrapper_root=str(root),
                      provider=provider, binary=binary, parent_context=str(parent.path),
                      parent_digest=parent.context_digest, child_context=str(child.path), child_digest=child.context_digest)
        if ready.get('standalone'):values['standalone'] = True
        path = base / 'task.json'
        if not path.exists():_write_private(path, dict(values, request_id=str(uuid4()), spawn_id=str(uuid4())))
        saved = read_task(base)
        if {key:saved.get(key) for key in values} != values:raise B1Error('FRESH_CHILD_INTENT_CONFLICT')
        if paths:await child.call('reserve_files', dict(paths=paths, ttl_seconds=ttl, exclusive=True), request_id=saved['request_id'])
        if prepare_only:return dict(prepared=True, running=False, native_ready=False, name=ready['name'])
        if (base / 'handoff.json').exists():return status(child, base)
        tmux = runpy.run_path(str(root / 'bin/lib/runtime_tmux.py'))
        if transport is None:transport = tmux['legacy_transport']()
        if transport.identity is None:raise B1Error('FRESH_CHILD_TMUX_PREPARE_REQUIRED')
        pane_name = 'orrery-' + child.context['client_id']
        command = shlex.join(['/usr/bin/env', 'AGENTSTACK_RUNTIME_MODE=fresh',
            'AGENTSTACK_FRESH_CLIENT_CONTEXT=' + str(child.path), 'AGENTSTACK_PYTHON=' + sys.executable,
            'AGENTSTACK_' + provider.upper() + '_BIN=' + binary,
            sys.executable, '-m', 'agentstack_mail.fresh_client', 'child-run', '--spawn-id', saved['spawn_id']])
        # Commit the attempt before the externally visible tmux operation. An
        # uncertain result is preserved and never automatically launches twice.
        _write_private(base / 'handoff.json', dict(spawn_id=saved['spawn_id'], socket_path=str(transport.path),
                                                session=pane_name))
        result = transport.command('new-session', '-d', '-s', pane_name, '-c', str(cwd), command)
        if result.returncode != 0:raise B1Error('FRESH_CHILD_LAUNCH_OUTCOME_UNKNOWN')
        return status(child, base)


def status(child, base):
    row = _read_private(base / 'handoff.json')
    current = child.path.parent / 'launch/current.json'
    result = dict(accepted=True, execution='unknown', native_ready=False, **identity(child))
    if current.exists():
        value = _read_private(current)
        run = child.path.parent / 'launch/runs' / native_id(value['launch_id'])
        intent = _read_private(run / 'intent.json');pinned(child, intent)
        task = read_task(base)
        if intent.get('child_spawn_id') != row['spawn_id'] or row['spawn_id'] != task['spawn_id']:
            raise B1Error('FRESH_CHILD_BINDING_MISMATCH')
        result['execution'] = 'ended' if (run / 'ended.json').exists() else 'started'
        if (run / 'session.json').exists():
            pinned(child, _read_private(run / 'session.json'));result['native_ready'] = True
    return result


def launch_task(client):
    parent, base, ready = family(client)
    if base is None:return None, None
    asyncio.run(verified_owner(parent))
    task = read_task(base)
    handoff_row = _read_private(base / 'handoff.json')
    if (task['child_context'] != str(client.path) or task['child_digest'] != client.context_digest
            or task['parent_context'] != str(parent.path) or task['parent_digest'] != parent.context_digest
            or handoff_row['spawn_id'] != task['spawn_id']):raise B1Error('FRESH_CHILD_BINDING_MISMATCH')
    if bool(task.get('standalone')) != bool(ready.get('standalone')):
        raise B1Error('FRESH_CHILD_BINDING_MISMATCH')
    reporting = ('This is a standalone task; there is no logical parent.\n\n' if ready.get('standalone') else
                 f"Report to the parent Mail agent ID {parent.context['mail_agent_id']} in this Mail instance.\n\n")
    prompt = (f"You are {ready['name']}. Registration and preparation are complete; no startup ritual. "
              + reporting + task['task'])
    return task, prompt


def main(operation_name, argv):
    parser = argparse.ArgumentParser()
    if operation_name == 'child-run':parser.add_argument('--spawn-id', required=True)
    else:
        parser.add_argument('--wrapper-root', required=True)
        parser.add_argument('--app-session-id');parser.add_argument('--app-agent-id')
        parser.add_argument('--child-client-name', required=operation_name == 'child-preregister')
        if operation_name == 'child-preregister':
            for field in ('name', 'program', 'model', 'task-description'):parser.add_argument('--' + field, required=True)
            parser.add_argument('--prepare-only', action='store_true')
            for field in ('manifest','backend','effort','helper-model'):parser.add_argument('--' + field)
            parser.add_argument('--url-index', type=int);parser.add_argument('--url-key')
        else:
            parser.add_argument('--pre-registered');parser.add_argument('--embed-task', action='store_true', required=True)
            parser.add_argument('--task-file', required=True);parser.add_argument('--resources', default='')
            parser.add_argument('--resource-ttl', type=int, default=14400)
            parser.add_argument('--codex', action='store_true');parser.add_argument('--model')
            parser.add_argument('--harness', choices=('dsh','hermes'));parser.add_argument('--backend')
            parser.add_argument('--unsafe-no-resources', action='store_true');parser.add_argument('--prepare-only', action='store_true')
            parser.add_argument('cwd', nargs='?', default=os.getcwd())
    args = parser.parse_args(argv)
    try:
        if (not os.environ.get('AGENTSTACK_FRESH_CLIENT_CONTEXT') or os.environ.get('AGENTSTACK_RUNTIME_MODE', 'legacy') not in ('legacy','fresh')
                or os.environ.get('AGENTSTACK_CLIENT_CONFIG') or os.environ.get('AGENTSTACK_GLOBAL_SERVER_CONFIG')):
            raise B1Error('FRESH_CONTEXT_REQUIRED')
        client = FreshBoundClient(os.environ['AGENTSTACK_FRESH_CLIENT_CONTEXT'])
        if operation_name == 'child-run':
            task, _ = launch_task(client)
            if task is None or task['spawn_id'] != args.spawn_id:raise B1Error('FRESH_CHILD_BINDING_MISMATCH')
            _, base, _ = family(client)
            with operation(base):
                try:_write_private(base / 'exec.json', {'spawn_id':args.spawn_id})
                except FileExistsError:raise B1Error('FRESH_CHILD_LAUNCH_OUTCOME_UNKNOWN') from None
            from .fresh_launch import launch
            return launch(client, task['wrapper_root'], task['provider'], task['cwd'])
        root = root_path(args.wrapper_root)
        transport=None
        app_runtime=bool(os.environ.get('AGENTSTACK_CODEX_APP_RUNTIME_DIR'))
        launch_id=os.environ.get('AGENTSTACK_FRESH_LAUNCH_ID')
        if app_runtime and launch_id:
            # A CLI child can inherit App settings. An arbitrary nonce must not
            # turn the Desktop template into a CLI parent.
            from .fresh_launch import layout
            base=layout(client);run=base/'runs'/native_id(launch_id)
            intent=_read_private(run/'intent.json');pinned(client,intent)
            receipt=_read_private(run/'session.json');pinned(client,receipt)
            if (_read_private(base/'current.json')!={'launch_id':launch_id}
                    or intent.get('nonce')!=os.environ.get('AGENTSTACK_FRESH_LAUNCH_NONCE')
                    or receipt.get('launch_id')!=launch_id or (run/'ended.json').exists()):
                raise B1Error('FRESH_LAUNCH_RECEIPT_MISMATCH')
        app_selected=app_runtime and not launch_id
        if app_selected:
            if not args.app_session_id:raise B1Error('FRESH_APP_REFERENCE_REQUIRED')
            # Import only the component in the trusted wrapper root, not HOME.
            source=str(root/'integrations/codex_app/src')
            sys.path.insert(0,source)
            from agentstack_codex_app.fresh_bridge import child_context
            client,transport=child_context(root,args.app_session_id,args.app_agent_id,
                need_transport=operation_name=='child-spawn' and not args.prepare_only)
        elif args.app_session_id is not None or args.app_agent_id is not None:
            raise B1Error('FRESH_APP_CONTEXT_REQUIRED')
        if operation_name == 'child-preregister':
            managed = None
            if args.program in {'dsh','hermes'}:
                from .fresh_harness import selection
                spec = dict(manifest_file=args.manifest, backend_id=args.backend)
                for field in ('effort','helper_model','url_index','url_key'):
                    value = getattr(args,field)
                    if value is not None:spec[field] = value
                runner, _, pins = selection(root, args.program, args.model, spec)
                spec.update({key:runner[key] for key in ('url_index','url_key','server_url') if key in runner})
                managed = dict(selection=spec,pins=pins)
            elif any(getattr(args,k) is not None for k in ('manifest','backend','effort','helper_model','url_index','url_key')):
                raise B1Error('FRESH_CHILD_ARGUMENTS_INVALID')
            result = asyncio.run(prepare(client, args.child_client_name, args.name, args.program, args.model, args.task_description, managed=managed))
        else:
            _, ready, _ = asyncio.run(selected_child(client, args.child_client_name, args.pre_registered))
            if (args.codex and ready['program'] != 'codex') or (args.model is not None and args.model != ready['model']):
                raise B1Error('FRESH_CHILD_INTENT_CONFLICT')
            if ((args.harness is not None and args.harness != ready['program'])
                    or (args.backend is not None and args.backend != ready.get('managed',{}).get('selection',{}).get('backend_id'))):
                raise B1Error('FRESH_CHILD_INTENT_CONFLICT')
            if Path(args.task_file).resolve(strict=True).is_relative_to(Path(client.context['root'])):
                raise B1Error('FRESH_CHILD_TASK_INVALID')
            result = asyncio.run(handoff(client, root, slot=args.child_client_name, name=args.pre_registered,
                cwd=args.cwd, task=task_text(args.task_file), resources=[p for p in args.resources.split(',') if p],
                ttl=args.resource_ttl, unsafe_no_resources=args.unsafe_no_resources, prepare_only=args.prepare_only,transport=transport))
        print(canonical(result));return 0
    except (B1Error, OSError, ValueError, TypeError, KeyError) as error:
        print('FRESH CHILD: ' + (error.code if isinstance(error, B1Error) else 'FRESH_CHILD_UNAVAILABLE'), file=sys.stderr)
        return 2
