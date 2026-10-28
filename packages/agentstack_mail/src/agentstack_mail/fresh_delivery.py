"""Existing watcher/tmux transport over protected fresh launch observations.

No new tmux server, window supervisor, identity registry or global activation.
The user prepares a pane and launches the ordinary fresh entry there. Signals
are hints; authenticated server pending/completion remain authoritative.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import runpy
import sys
from uuid import uuid4

from .fresh_launch import identity, layout, native_id, pinned, root_path
from .fresh_notifications import IMPORTANCE, consumer_lock
from .fresh_runtime import _directory, _read_private, _write_private, read_config
from .fresh_proxy import verified_owner
from .storage_origin_contract import B1Error, digest


def helpers(root):
    return (runpy.run_path(str(root / 'bin/lib/runtime_tmux.py')),
            runpy.run_path(str(root / 'bin/lib/process_observation.py')),
            runpy.run_path(str(root / 'bin/lib/runtime_delivery.py')))


def require_birth(api, pid):
    value = api['process_start'](pid)
    if value in (None, 'absent'):raise B1Error('FRESH_TMUX_PROCESS_UNAVAILABLE')
    return value


def capture(client, wrapper_root, launch_id):
    """Observe only the host-selected endpoint; never search default sockets."""
    raw, pane = os.environ.get('TMUX'), os.environ.get('TMUX_PANE')
    if raw is None and pane is None:return None
    if not raw or not pane or len(raw.rsplit(',', 2)) != 3:
        raise B1Error('FRESH_TMUX_CONTEXT_REQUIRED')
    tmux, processes, _ = helpers(wrapper_root)
    try:
        transport = tmux['Transport'](Path(raw.rsplit(',', 2)[0]))
        actual = tmux['panes'](transport, pane=pane)
        if len(actual) != 1:raise B1Error('FRESH_TMUX_TARGET_CHANGED')
        row = actual[0]
        runner = dict(pid=os.getpid(), process_start=require_birth(processes, os.getpid()))
        pane_start = require_birth(processes, row['pid'])
        if not processes['belongs_to'](runner['pid'], row['pid'], os.getuid()):
            raise B1Error('FRESH_TMUX_TARGET_CHANGED')
        return dict(kind='orrery-fresh-tmux-route-v1', context_digest=client.context_digest,
            launch_id=launch_id, socket_path=str(transport.path), socket_identity=list(transport.identity),
            pane={key:row[key] for key in ('session_id','pane_id','pid')},
            pane_process_start=pane_start, runner=runner, **identity(client))
    except tmux['TargetError'] as error:
        raise B1Error(str(error)) from None


def publish(run, captured, process, wrapper_root):
    if captured is None:return
    _, processes, _ = helpers(wrapper_root)
    provider = dict(pid=process.pid, process_start=require_birth(processes, process.pid))
    if not processes['belongs_to'](process.pid, captured['runner']['pid'], os.getuid()):
        raise B1Error('FRESH_TMUX_TARGET_CHANGED')
    _write_private(run / 'tmux.json', dict(captured, provider=provider))


def route(client):
    """Read a single current run, never the newest name/ID or another context."""
    base = layout(client)
    current = _read_private(base / 'current.json')
    if set(current) != {'launch_id'}:raise B1Error('FRESH_TMUX_TARGET_CHANGED')
    run = base / 'runs' / native_id(current['launch_id']);_directory(run)
    if (run / 'ended.json').exists():raise B1Error('FRESH_TMUX_TARGET_ENDED')
    intent, observed, row = (_read_private(run / name) for name in ('intent.json','session.json','tmux.json'))
    for value in (intent, observed, row):pinned(client, value)
    if (row.get('kind') != 'orrery-fresh-tmux-route-v1'
            or any(value.get('launch_id') != current['launch_id'] for value in (intent,observed,row))
            or observed.get('provider') != intent.get('provider')):
        raise B1Error('FRESH_TMUX_TARGET_CHANGED')
    if intent.get('provider') in {'dsh','hermes'}:
        from .fresh_harness import native_session_id
        session = native_session_id(observed.get('native_session_id'))
    else:session = native_id(observed.get('native_session_id'))
    saved = _read_private(base / 'sessions' / (session + '.json'))
    if observed != dict(saved, launch_id=current['launch_id']):raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
    return row, intent


def target(client, wrapper_root, expected=None, *, readiness=True):
    row, intent = route(client)
    if expected is not None and row != expected:raise B1Error('FRESH_TMUX_TARGET_CHANGED')
    tmux, processes, _ = helpers(wrapper_root)
    try:
        transport = tmux['Transport'](Path(row['socket_path']))
        if list(transport.identity) != row['socket_identity']:raise B1Error('FRESH_TMUX_TARGET_CHANGED')
        actual = tmux['panes'](transport, pane=row['pane']['pane_id'])
        if len(actual) != 1 or any(actual[0][key] != value for key,value in row['pane'].items()):
            raise B1Error('FRESH_TMUX_TARGET_CHANGED')
        def births():
            snapshot = processes['process_snapshot']()
            expected = [(row['pane']['pid'], row['pane_process_start'])] + [
                (row[role]['pid'], row[role]['process_start']) for role in ('runner', 'provider')]
            return snapshot is not None and all(
                pid in snapshot and snapshot[pid][2] == birth for pid, birth in expected)
        def ancestry():
            snapshot = processes['process_snapshot']()
            return (processes['snapshot_belongs_to'](snapshot, row['runner']['pid'], row['pane']['pid'], os.getuid())
                and processes['snapshot_belongs_to'](snapshot, row['provider']['pid'], row['runner']['pid'], os.getuid()))
        if (not births()
                or not ancestry()
                or not births()):raise B1Error('FRESH_TMUX_TARGET_CHANGED')
        if readiness:
            screen = transport.command('capture-pane','-p','-t',row['pane']['pane_id'],'-S','-25')
            if screen.returncode:raise B1Error('FRESH_TMUX_TARGET_NOT_READY')
            if intent['provider'] in {'dsh','hermes'}:
                from .fresh_harness import readiness as managed_readiness
                state = managed_readiness(wrapper_root, screen.stdout, intent['provider'])
            else:state = tmux['compact_readiness'](screen.stdout, intent['provider'])
            # Work is allowed: existing native CLIs queue input while working.
            # Bare/unknown/approval screens and preexisting input are not targets.
            if state['state'] not in {'idle','work'} or state['input_empty'] is not True:
                raise B1Error('FRESH_TMUX_TARGET_NOT_READY')
        if route(client)[0] != row or not births():raise B1Error('FRESH_TMUX_TARGET_CHANGED')
        return transport, row
    except tmux['TargetError'] as error:
        raise B1Error(str(error)) from None


def attempt_path(client, mid):
    folder = Path(client.context['root']) / 'notification-consumers' / str(client.context['mail_agent_id']) / 'tmux-attempts'
    folder.mkdir(mode=0o700, exist_ok=True);_directory(folder)
    return folder / (str(mid) + '.json')


def save_attempt(path, value):
    temporary = path.parent / ('.attempt-' + str(uuid4()))
    _write_private(temporary, value)
    os.replace(temporary, path)


async def deliver(client, wrapper_root):
    await verified_owner(client)
    with consumer_lock(client) as acquired:
        if not acquired:return {'status':'busy','completed':0}
        if os.environ.get('AGENTSTACK_MAIL_DISABLED') == '1':return {'status':'disabled','completed':0}
        transport, captured = target(client, wrapper_root)
        _, _, api = helpers(wrapper_root)
        minimum = os.environ.get('AGENTSTACK_MAIL_NOTIFY_MIN_IMPORTANCE','low').lower()
        if minimum not in IMPORTANCE:minimum = 'normal'
        batch = await client.call('notification_pending', {'min_importance':minimum})
        if any(batch.get(k) != value for k,value in identity(client).items()):
            raise B1Error('FRESH_PROXY_BINDING_MISMATCH')
        count = 0
        for message in batch['messages']:
            mid = message['id'];path = attempt_path(client, mid)
            stamp = datetime.now(timezone.utc)
            if path.exists() or path.is_symlink():
                previous = _read_private(path)
                if previous.get('message_id') != mid or any(previous.get(k) != v for k,v in identity(client).items()):
                    raise B1Error('FRESH_DELIVERY_ATTEMPT_CHANGED')
                if stamp < datetime.fromisoformat(previous['not_before']):continue
            exact = await client.call('notification_pending', {'message_id':mid,'limit':1,'min_importance':minimum})
            if any(exact.get(k) != v for k,v in identity(client).items()):raise B1Error('FRESH_PROXY_BINDING_MISMATCH')
            if not exact['messages']:
                if path.exists():path.unlink()
                continue
            transport, _ = target(client, wrapper_root, captured)
            selected = api['policy']('tmux')
            attempt = dict(message_id=mid, launch_id=captured['launch_id'], attempt_id=str(uuid4()),
                not_before=(stamp+timedelta(seconds=max(selected['lease'],selected['backoff'](1)))).isoformat(),
                **identity(client))
            save_attempt(path, attempt)  # A retry bound, never a delivery-success cache.
            buffer = 'orrery-' + attempt['attempt_id']
            try:
                loaded = transport.command('load-buffer','-b',buffer,'-',input=api['format_notification'](exact['messages'][0]))
                if loaded.returncode:raise B1Error('FRESH_TMUX_LOAD_FAILED')
                transport, _ = target(client, wrapper_root, captured)
                pasted = transport.command('paste-buffer','-d','-p','-b',buffer,'-t',captured['pane']['pane_id'])
                if pasted.returncode:raise B1Error('FRESH_TMUX_PASTE_UNKNOWN')
                transport, _ = target(client, wrapper_root, captured, readiness=False)
                submitted = transport.command('send-keys','-t',captured['pane']['pane_id'],'C-m')
                if submitted.returncode:raise B1Error('FRESH_TMUX_SUBMIT_UNKNOWN')
                await client.call('notification_complete', {'message_id':mid})
                path.unlink();count += 1
            finally:
                # Only our random buffer, only the pinned endpoint; no fallback.
                try:transport.command('delete-buffer','-b',buffer)
                except (OSError,RuntimeError):pass
        return {'status':'ok','completed':count}


def config(path):
    return read_config(path)


async def watch_once(config_path, wrapper_root):
    from .fresh_client import FreshBoundClient
    root, original = config(config_path)
    parent = root/'clients'
    if not parent.exists():return {'ok':True,'routes':[]}
    _directory(parent)
    folders = list(parent.iterdir())
    if len(folders) > 1000:raise B1Error('FRESH_DELIVERY_ROUTE_LIMIT')
    groups, results = {}, []
    for folder in sorted(folders):
        if re.fullmatch(r'[0-9a-f-]{36}',folder.name) is None:continue
        try:
            _directory(folder)
            client = FreshBoundClient(folder/'context.json')
            if client.context['mail_instance_id'] != original['instance_id']:raise B1Error('INSTANCE_MISMATCH')
            base = folder/'launch'
            if not (base/'current.json').exists():continue
            current = _read_private(base/'current.json')
            run = base/'runs'/native_id(current.get('launch_id'))
            if (run/'ended.json').exists() or not (run/'tmux.json').exists():continue
            row, _ = route(client)
            groups.setdefault(client.context['mail_agent_id'],[]).append(client)
        except (B1Error,OSError,ValueError,TypeError,KeyError) as error:
            results.append({'client_id':folder.name,'reason':error.code if isinstance(error,B1Error) else 'FRESH_ROUTE_UNAVAILABLE'})
    for aid, clients in groups.items():
        if config(config_path)[1] != original:raise B1Error('FRESH_WATCH_CONFIG_CHANGED')
        if len(clients) != 1:
            results.append({'mail_agent_id':aid,'reason':'FRESH_DELIVERY_ROUTE_AMBIGUOUS'});continue
        try:
            result = await deliver(clients[0], wrapper_root)
            results.append(dict(result, mail_agent_id=aid))
        except (B1Error,OSError,ValueError,TypeError,KeyError,RuntimeError) as error:
            results.append({'mail_agent_id':aid,'reason':error.code if isinstance(error,B1Error) else 'FRESH_ROUTE_UNAVAILABLE'})
    return {'ok':not any(item.get('reason') for item in results),'routes':results}


def watch_service(config_path, wrapper_root, *, once=False):
    wrapper_root = root_path(wrapper_root)
    root, original = config(config_path)
    if once:return asyncio.run(watch_once(config_path, wrapper_root))
    _, _, api = helpers(wrapper_root)
    def tick():
        if config(config_path)[1] != original:raise B1Error('FRESH_WATCH_CONFIG_CHANGED')
        result = asyncio.run(watch_once(config_path, wrapper_root))
        for item in result['routes']:
            if item.get('reason'):print('fresh-mail-watcher: '+item['reason'],file=sys.stderr)
    tick()
    api['watch_loop'](root/'signals', {'safe_private_directory':_directory}, tick)
