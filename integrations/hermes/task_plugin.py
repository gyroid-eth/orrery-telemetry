"""Synchronous reservation gate with private profile memory and skills preserved."""
import hashlib
import os
from pathlib import Path

from mail_lifecycle import read, save, stamp
from mail_probe import mail_receipt
from reservation_guard import create_guard, require_guard
from task_evidence import append, event

MAIL_TOOL = 'mcp__orrery_mail__send_message'
PRIVATE_TOOLS = {'memory', 'skills_list', 'skill_view', 'skill_manage', 'session_search'}


def install(ctx, state):
    state = Path(state)
    config, attempt, task = [read(state / name) for name in ('run-config.json', 'attempt.json', 'task.json')]
    profile = Path(config['profile_home']).resolve()
    if Path(os.environ.get('HERMES_HOME', '')).resolve() != profile or config['purpose'] != 'file-task':
        raise ValueError('Managed task profile mismatch')
    if task.get('reservation_source') != 'product-bound-proxy' or not task.get('reservation_receipts'):
        raise ValueError('Real reservation receipts required')
    expected_mail = read(state / 'message.json')
    seen_read = set()
    scope = lambda row: append(state, 'events.jsonl', row)
    file_guard = create_guard(attempt, task, scope)

    def deny(tool, reason):
        event(state, 'tool-denied', tool=tool, reason=reason)
        return {'action': 'block', 'message': reason}

    def before(tool_name, args, **_):
        try:
            if args.get('profile') not in (None, '', config['profile_name']):
                return deny(tool_name, 'outside-current-profile')
            if tool_name in ('read_file', 'write_file'):
                target = Path(args.get('path', '')).resolve()
                if Path(args.get('path', '')).is_absolute() and any(
                        target.is_relative_to(profile / name) for name in ('skills', 'memories')):
                    event(state, 'tool-allowed', tool=tool_name, private_profile=True)
                    return None
                return file_guard(tool_name, args)
            if tool_name in PRIVATE_TOOLS:
                if tool_name in ('skill_view', 'skill_manage'):
                    for key in ('name', 'path', 'category'):
                        value = args.get(key)
                        if isinstance(value, str) and (Path(value).is_absolute() or '..' in Path(value).parts):
                            return deny(tool_name, 'outside-current-profile')
                return None
            if tool_name != MAIL_TOOL:
                return deny(tool_name, 'not-in-wire-tools')
            if args != expected_mail:
                return deny(tool_name, 'unexpected-mail-arguments')
            for spec in task['expected']:
                target = (Path(task['worktree']) / spec['path']).resolve()
                if (str(target) not in seen_read or not target.is_file()
                        or target.stat().st_mtime_ns < attempt['started_at_ns']
                        or spec['contains'].encode() not in target.read_bytes()):
                    return deny(tool_name, 'artifact-not-ready')
            save(state / 'report-intent.json', {'attempt_id': attempt['attempt_id'],
                 'agent_name': attempt['agent_name'], 'at': stamp()}, exclusive=True)
            event(state, 'tool-allowed', tool=tool_name)
            return None
        except Exception:
            return deny(tool_name, 'managed-guard-error')

    before.orrery_guard_attempt = attempt['attempt_id']
    before.orrery_guard_task = task['task_id']

    def after(tool_name, result, args=None, status=None, **_):
        args = args or {}
        if tool_name == 'read_file' and status == 'ok':
            seen_read.add(str(Path(args['path']).resolve()))
        if tool_name == 'write_file' and status == 'ok':
            target = Path(args['path']).resolve()
            if target.is_file() and target.is_relative_to(Path(task['worktree']).resolve()):
                event(state, 'artifact-written', path=target.relative_to(Path(task['worktree']).resolve()).as_posix(),
                      sha256=hashlib.sha256(target.read_bytes()).hexdigest())
        if tool_name == MAIL_TOOL:
            receipt = mail_receipt(result)
            if receipt:
                valid = (receipt['ok'] and receipt['from'] == attempt['agent_name']
                         and receipt['to'] in (task['report_to'], [task['report_to']]))
                save(state / 'mail-send-receipt.json', {**receipt, 'ok': bool(valid),
                     'attempt_id': attempt['attempt_id'], 'agent_name': attempt['agent_name'],
                     'source': 'product-bound-proxy', 'checked_at': stamp()}, exclusive=True)
                event(state, 'mail-sent', **{**receipt, 'ok': bool(valid)})

    ctx.register_hook('pre_tool_call', before)
    ctx.register_hook('post_tool_call', after)
    event(state, 'plugin-ready', tools=['read_file', 'write_file', MAIL_TOOL], runtime_phase=attempt['phase'])
    event(state, 'task-start')


def assert_registered(manager, state):
    state = Path(state)
    attempt, task = read(state / 'attempt.json'), read(state / 'task.json')
    callbacks = list(manager.iter_hook_callbacks('pre_tool_call'))
    required = [cb for cb in callbacks if getattr(cb, 'orrery_guard_attempt', None) == attempt['attempt_id']
                and getattr(cb, 'orrery_guard_task', None) == task['task_id']]
    if len(required) != 1 or len(callbacks) != 1:
        raise RuntimeError('Exactly the required managed reservation callback must be loaded')
    require_guard(manager, required[0])
    return required[0]


def register(ctx):
    state = os.environ.get('ORRERY_HERMES_STATE_ROOT')
    if not state or not Path(state).is_absolute():
        raise ValueError('Explicit managed state required')
    install(ctx, state)
