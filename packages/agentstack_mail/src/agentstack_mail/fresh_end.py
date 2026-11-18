"""Fresh run closure: observed exit and operator resolution stay distinct.

No process termination, registration, repair, or inferred exit status. The
resolver observes only the immutable run's recorded endpoint and job groups.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import time

from .fresh_runtime import _directory, _read_private, _write_private
from .storage_origin_contract import B1Error, digest

KIND = 'orrery-fresh-resolved-ended-v1'
GROUPS_KIND = 'orrery-fresh-process-groups-v1'


@dataclass(frozen=True)
class RunEnd:
    kind: str  # normal (including a failed exit), or resolved (exit unknown)
    exit_code: int | None
    record: dict


def present(path):
    # lstat distinguishes absence from permission failure, including symlinks.
    try:path.lstat()
    except FileNotFoundError:return False
    return True


def run_directory(client, run):
    from .fresh_launch import native_id
    expected = client.path.parent / 'launch' / 'runs' / native_id(run.name)
    if run != expected or run.resolve() != run:raise B1Error('FRESH_LAUNCH_BINDING_MISMATCH')
    for path in (run.parent.parent, run.parent, run):_directory(path)


def validate_intent(client, run, intent):
    from .fresh_launch import pinned
    pinned(client, intent)
    if (intent.get('kind') != 'orrery-fresh-launch-v1' or intent.get('launch_id') != run.name
            or intent.get('provider') not in {'claude', 'codex', 'dsh', 'hermes'}
            or not isinstance(intent.get('cwd'), str) or not Path(intent['cwd']).is_absolute()
            or not isinstance(intent.get('nonce'), str) or not intent['nonce']
            or 'resume_session_id' not in intent
            or intent['resume_session_id'] is not None and not isinstance(intent['resume_session_id'], str)):
        raise B1Error('FRESH_LAUNCH_BINDING_MISMATCH')


def read_run_end(client, run):
    """Read both closure types, accepting the original two-field ended format."""
    run_directory(client, run)
    normal, resolved = (present(run / name) for name in ('ended.json', 'resolved-ended.json'))
    if normal and resolved:raise B1Error('FRESH_LAUNCH_OUTCOME_UNKNOWN')
    if normal:
        value = _read_private(run / 'ended.json')
        if (set(value) != {'launch_id', 'exit_code'} or value['launch_id'] != run.name
                or type(value['exit_code']) is not int):raise B1Error('FRESH_LAUNCH_OUTCOME_UNKNOWN')
        return RunEnd('normal', value['exit_code'], value)
    if not resolved:return None
    from .fresh_launch import identity, pinned
    value = _read_private(run / 'resolved-ended.json')
    intent = _read_private(run / 'intent.json');validate_intent(client, run, intent)
    route = _read_private(run / 'tmux.json');pinned(client, route)
    keys = {'kind', 'launch_id', 'context_digest', *identity(client), 'intent_digest',
            'route_digest', 'reason', 'confirmed_at', 'provider_exit_code', 'observation'}
    pinned(client, value)
    try:
        stamp = datetime.fromisoformat(value['confirmed_at'])
        valid_stamp = stamp.utcoffset() is not None
    except (KeyError, TypeError, ValueError):valid_stamp = False
    if (set(value) != keys or value.get('kind') != KIND or value.get('launch_id') != run.name
            or intent.get('launch_id') != run.name or route.get('launch_id') != run.name
            or value.get('intent_digest') != digest(intent) or value.get('route_digest') != digest(route)
            or value.get('reason') != 'operator-confirmed-ended' or not valid_stamp
            or value.get('provider_exit_code') is not None
            or not valid_observation(route, value.get('observation'))):
        raise B1Error('FRESH_LAUNCH_OUTCOME_UNKNOWN')
    return RunEnd('resolved', None, value)


def process_record(value):
    return (isinstance(value, dict) and set(value) == {'pid', 'process_start'}
            and type(value['pid']) is int and value['pid'] > 0
            and isinstance(value['process_start'], str) and 0 < len(value['process_start']) <= 128
            and value['process_start'] != 'absent')


def recorded(route):
    """Versioned additive field; older records without group proof stay HOLD."""
    from .fresh_targets import Targets
    Targets.validate_display_route(route)
    value = route.get('process_observation')
    try:
        if (not isinstance(value, dict) or set(value) != {'kind', 'groups', 'members'}
                or value['kind'] != GROUPS_KIND or not isinstance(value['groups'], list)
                or not 1 <= len(value['groups']) <= 3 or not isinstance(value['members'], list)
                or not 1 <= len(value['members']) <= 64):raise ValueError
        groups = value['groups'];members = value['members']
        for group in groups:
            if (set(group) != {'pgid', 'leader_start'} or type(group['pgid']) is not int or group['pgid'] < 1
                    or not isinstance(group['leader_start'], str) or not 0 < len(group['leader_start']) <= 128
                    or group['leader_start'] == 'absent'):raise ValueError
        if len({g['pgid'] for g in groups}) != len(groups) or not all(process_record(m) for m in members):raise ValueError
        required = [route['runner'], route['provider'],
                    dict(pid=route['pane']['pid'], process_start=route['pane_process_start'])]
        if any(row not in members for row in required):raise ValueError
        if len({m['pid'] for m in members}) != len(members):raise ValueError
        if any(dict(pid=g['pgid'], process_start=g['leader_start']) not in members for g in groups):raise ValueError
        return groups, members
    except (KeyError, TypeError, ValueError):
        raise B1Error('FRESH_RESOLVE_RECORD_INSUFFICIENT') from None


def valid_observation(route, value):
    try:
        groups, members = recorded(route)
        return (isinstance(value, dict) and set(value) == {'processes', 'groups', 'pane'}
                and value['processes'] == [dict(m, state='absent') for m in members]
                and value['groups'] == [dict(g, state='empty') for g in groups]
                and value['pane'] in ('absent', 'dead'))
    except B1Error:return False


def capture_groups(processes, route):
    """Capture PID/birth and foreground groups once, without changing role shapes."""
    rows = processes['process_snapshot'](include_groups=True)
    if rows is None:raise B1Error('FRESH_TMUX_PROCESS_UNAVAILABLE')
    children = {}
    for pid, row in rows.items():
        if (not isinstance(row, (tuple, list)) or len(row) != 4
                or not process_record(dict(pid=pid, process_start=row[2]))
                or any(type(row[i]) is not int or row[i] < 0 for i in (0, 1, 3))):
            raise B1Error('FRESH_TMUX_PROCESS_UNAVAILABLE')
        children.setdefault(row[0], []).append(pid)
    members = []
    for member in (route['runner'], route['provider'],
                   dict(pid=route['pane']['pid'], process_start=route['pane_process_start'])):
        row = rows.get(member['pid'])
        if (row is None or row[1:3] != (os.getuid(), member['process_start'])
                or len(row) != 4):raise B1Error('FRESH_TMUX_TARGET_CHANGED')
        if member not in members:members.append(member)
    groups = []
    for member in (route['runner'], route['provider']):
        pgid = rows[member['pid']][3]
        leader = rows.get(pgid)
        if leader is None or leader[1] != os.getuid() or leader[3] != pgid:
            raise B1Error('FRESH_TMUX_PROCESS_UNAVAILABLE')
        group = dict(pgid=pgid, leader_start=leader[2])
        if group not in groups:groups.append(group)
        leader_record = dict(pid=pgid, process_start=leader[2])
        if leader_record not in members:members.append(leader_record)
    # Keep every descendant already visible in this snapshot, even if it has
    # left the foreground group or changes group after publication. No history
    # watcher: children first created after this snapshot remain outside it.
    seen = {route['provider']['pid']}
    pending = list(children.get(route['provider']['pid'], []))
    while pending:
        pid = pending.pop()
        if pid in seen or rows[pid][1] != os.getuid():raise B1Error('FRESH_TMUX_PROCESS_UNAVAILABLE')
        seen.add(pid)
        member = dict(pid=pid, process_start=rows[pid][2])
        if member not in members:members.append(member)
        if len(members) > 64:raise B1Error('FRESH_TMUX_PROCESS_UNAVAILABLE')
        pending.extend(children.get(pid, []))
    return dict(kind=GROUPS_KIND, groups=groups, members=members)


def observe_ended(route, wrapper_root, *, deadline):
    from .fresh_delivery import helpers, parse_panes
    tmux, processes, _ = helpers(wrapper_root)
    groups, members = recorded(route)
    if time.monotonic() >= deadline:raise B1Error('FRESH_RESOLVE_OBSERVATION_UNAVAILABLE')
    try:rows = processes['process_snapshot'](include_groups=True, deadline=deadline)
    except (OSError, RuntimeError):raise B1Error('FRESH_RESOLVE_OBSERVATION_UNAVAILABLE') from None
    if rows is None:raise B1Error('FRESH_RESOLVE_OBSERVATION_UNAVAILABLE')
    for member in members:
        pid, birth = member['pid'], member['process_start']
        try:actual = processes['process_start'](pid, deadline=deadline)
        except (OSError, RuntimeError):raise B1Error('FRESH_RESOLVE_OBSERVATION_UNAVAILABLE') from None
        if actual is None:raise B1Error('FRESH_RESOLVE_OBSERVATION_UNAVAILABLE')
        if pid in rows or actual != 'absent':
            if actual == birth and pid in rows and rows[pid][1:3] == (os.getuid(), birth):
                raise B1Error('FRESH_RESOLVE_PROCESS_ALIVE')
            raise B1Error('FRESH_RESOLVE_PROCESS_CHANGED')
    for group in groups:
        if any(len(row) != 4 for row in rows.values()):raise B1Error('FRESH_RESOLVE_OBSERVATION_UNAVAILABLE')
        if any(row[3] == group['pgid'] for row in rows.values()):raise B1Error('FRESH_RESOLVE_GROUP_ALIVE')
    path = Path(route['socket_path'])
    if not path.is_absolute() or path.resolve() != path:raise B1Error('FRESH_RESOLVE_TARGET_CHANGED')
    try:
        if not present(path):pane = 'absent'
        else:
            transport = tmux['Transport'](path)
            if list(transport.identity) != route['socket_identity']:raise B1Error('FRESH_RESOLVE_TARGET_CHANGED')
            # List all panes: a missing selected pane is not a tmux command error.
            result = transport.command('list-panes', '-a', '-F',
                '#{session_id}\t#{pane_id}\t#{pane_pid}\t#{pane_dead}', deadline=deadline)
            if result.returncode or len(result.stdout.encode()) > 262144:
                raise B1Error('FRESH_RESOLVE_OBSERVATION_UNAVAILABLE')
            try:panes = parse_panes(result.stdout)
            except ValueError:raise B1Error('FRESH_RESOLVE_OBSERVATION_UNAVAILABLE') from None
            found = None
            for actual in panes:
                if actual['pane_id'] == route['pane']['pane_id']:
                    # tmux 3.8 omits pane_pid when its PTY fd is closed. Only
                    # an explicitly dead pane may omit it; recorded births and
                    # groups above must already be absent, on both observations.
                    if (actual['session_id'] != route['pane']['session_id']
                            or actual['pid'] not in (route['pane']['pid'], None)):
                        raise B1Error('FRESH_RESOLVE_TARGET_CHANGED')
                    found = actual['dead']
            if found is False:raise B1Error('FRESH_RESOLVE_PROCESS_ALIVE')
            if tmux['validate_socket'](path) != transport.identity:raise B1Error('FRESH_RESOLVE_TARGET_CHANGED')
            pane = 'dead' if found is True else 'absent'
    except tmux['TargetError']:
        raise B1Error('FRESH_RESOLVE_OBSERVATION_UNAVAILABLE') from None
    if time.monotonic() >= deadline:raise B1Error('FRESH_RESOLVE_OBSERVATION_UNAVAILABLE')
    return dict(processes=[dict(m, state='absent') for m in members],
                groups=[dict(g, state='empty') for g in groups], pane=pane)


def resolve_ended(client, wrapper_root, launch_id, *, confirm_ended=False):
    from .fresh_launch import identity, layout, native_id, operation, pinned, root_path
    from .fresh_proxy import verified_owner
    if confirm_ended is not True:raise B1Error('FRESH_RESOLVE_CONFIRM_REQUIRED')
    launch_id = native_id(launch_id)
    root = root_path(wrapper_root)
    asyncio.run(verified_owner(client))
    base = layout(client)
    with operation(base):
        def records():
            client._load()
            if _read_private(base / 'current.json') != {'launch_id': launch_id}:
                raise B1Error('FRESH_RESOLVE_NOT_CURRENT')
            run = base / 'runs' / launch_id;run_directory(client, run)
            intent = _read_private(run / 'intent.json');validate_intent(client, run, intent)
            if intent.get('wrapper_root') != str(root):
                raise B1Error('FRESH_LAUNCH_BINDING_MISMATCH')
            return run, intent
        run, intent = records()
        ended = read_run_end(client, run)
        if ended is not None:
            return dict(ok=True, launch_id=launch_id, end_kind=ended.kind, provider_exit_code=ended.exit_code)
        route = _read_private(run / 'tmux.json');pinned(client, route)
        if route.get('launch_id') != launch_id:raise B1Error('FRESH_LAUNCH_BINDING_MISMATCH')
        deadline = time.monotonic() + 10
        observation = observe_ended(route, root, deadline=deadline)
        # Reauthenticate and reread immutable records while holding the launch mutex.
        asyncio.run(verified_owner(client))
        if records() != (run, intent) or _read_private(run / 'tmux.json') != route:
            raise B1Error('FRESH_RESOLVE_TARGET_CHANGED')
        if read_run_end(client, run) is not None:raise B1Error('FRESH_RESOLVE_TARGET_CHANGED')
        if observe_ended(route, root, deadline=deadline) != observation:
            raise B1Error('FRESH_RESOLVE_TARGET_CHANGED')
        if records() != (run, intent) or _read_private(run / 'tmux.json') != route:
            raise B1Error('FRESH_RESOLVE_TARGET_CHANGED')
        value = dict(kind=KIND, launch_id=launch_id, context_digest=client.context_digest,
            intent_digest=digest(intent), route_digest=digest(route), reason='operator-confirmed-ended',
            confirmed_at=datetime.now(timezone.utc).isoformat(), provider_exit_code=None,
            observation=observation, **identity(client))
        _write_private(run / 'resolved-ended.json', value)
        return dict(ok=True, launch_id=launch_id, end_kind='resolved', provider_exit_code=None)
