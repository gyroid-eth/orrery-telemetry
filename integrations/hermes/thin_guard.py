"""Native Hermes pre-tool adapter; decisions stay in the common product hooks.

This is the same mistake guard as the other clients, not a shell sandbox.
Loading this plugin is Hermes's responsibility; no separate launch preflight.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import NamedTuple


class HookResult(NamedTuple):
    allowed: bool
    reason: str = ''

    def __bool__(self):
        return self.allowed


def _denial_reason(result):
    if result.returncode != 2:
        return ''
    # Accept the public hook reason schema; arbitrary stdout is not a reason.
    try:
        public = json.loads(result.stdout or '{}')
    except json.JSONDecodeError:
        public = None
    reason = public.get('reason') if isinstance(public, dict) else None
    if not isinstance(reason, str) or not reason.strip():
        specific = public.get('hookSpecificOutput') if isinstance(public, dict) else None
        reason = specific.get('permissionDecisionReason') if isinstance(specific, dict) else None
    if not isinstance(reason, str) or not reason.strip():
        reason = getattr(result, 'stderr', '') or ''
    # Diagnose the visible text, including errors decorated by a terminal.
    reason = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', reason)
    if re.search(r'(?m)^\s*(?:Traceback \(|File ".*", line \d+|\w*(?:Error|Exception):)|: line \d+:', reason):
        return ''
    # Like Claude's blocking hook stderr, this is intended for the caller.
    # Bound the message and remove terminal control sequences from tool output.
    return ''.join(char for char in reason.strip() if char.isprintable() or char in '\n\t')[:2000]


def payloads(tool_name, args, *, cwd, session_id=''):
    """Map native file edits, including both endpoints of a V4A move."""
    base = {'cwd': cwd, 'session_id': session_id}
    name = {'terminal': 'Bash', 'write_file': 'Write', 'patch': 'Edit',
            'read_file': 'Read'}.get(tool_name, tool_name)
    inputs = [dict(args)]
    if tool_name == 'patch' and args.get('mode') == 'patch':
        text = args.get('patch') or ''
        paths = re.findall(r'^\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*(.+)$', text, re.M)
        for source, target in re.findall(r'^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)$', text, re.M):
            paths.extend((source, target))
        if args.get('path'):
            paths.append(args['path'])
        inputs = [{'path': path.strip()} for path in dict.fromkeys(paths)]
    for item in inputs:
        # Resolve like Hermes's host file tools, without changing their arguments.
        if isinstance(item.get('path'), str):
            target = Path(item['path']).expanduser()
            item['file_path'] = str((Path(cwd) / target).resolve())
        yield {**base, 'tool_name': name, 'tool_input': item}


def _cwd(tool_name, args, task_id):
    # File tools follow Hermes's per-task terminal cwd, not necessarily os.getcwd.
    from tools.file_tools_paths import _resolve_base_dir
    cwd = str(_resolve_base_dir(task_id or 'default'))
    if tool_name == 'terminal' and args.get('workdir'):
        cwd = str((Path(cwd) / Path(args['workdir']).expanduser()).resolve())
    return cwd


def call_hook(name, payload):
    root = Path(os.environ['AGENTSTACK_HOME'])
    interpreter = sys.executable if name.endswith('.py') else '/bin/bash'
    result = subprocess.run([interpreter, str(root / 'hooks' / name)],
                            input=json.dumps(payload), text=True, capture_output=True,
                            cwd=payload['cwd'], timeout=60, errors='replace')
    if result.returncode == 0:
        # Only the common hook's public warning schema is visible. Raw stdout
        # and stderr are not forwarded for successful hooks.
        try:
            public = json.loads(getattr(result, 'stdout', '') or '{}')
        except json.JSONDecodeError:
            public = None
        if isinstance(public, dict) and isinstance(public.get('systemMessage'), str) and public['systemMessage']:
            print(public['systemMessage'], file=sys.stderr)
    return HookResult(result.returncode == 0, _denial_reason(result) if result.returncode else '')


def before(tool_name, args, session_id='', task_id='', **_):
    cwd = _cwd(tool_name, args, task_id)
    for payload in payloads(tool_name, args, cwd=cwd, session_id=session_id):
        checks = ['deny-llm-key-read.py']
        if payload['tool_name'] in ('Bash', 'Write', 'Edit'):
            checks.append('check-agent-registered.sh')
        if payload['tool_name'] in ('Write', 'Edit'):
            checks.append('check-file-reservation.sh')
        for name in checks:
            decision = call_hook(name, payload)
            if not decision:
                reason = getattr(decision, 'reason', '')
                return {'action': 'block', 'message': 'ORRERY hook denied: ' + name +
                        ('\n' + reason if reason else '')}
    return None


def after(tool_name, result, args=None, status=None, session_id='', task_id='', **_):
    # Hermes derives ok/error from the native result before emitting this hook.
    # Never pass tool output (which can contain credentials) to release hooks.
    if status != 'ok' or tool_name not in ('write_file', 'patch'):
        return
    args = args or {}
    cwd = _cwd(tool_name, args, task_id)
    for payload in payloads(tool_name, args, cwd=cwd, session_id=session_id):
        call_hook('release-file-reservation.sh', {**payload, 'tool_result': {'success': True}})


def session_started(session_id, **_):
    """Save only this CLI's event, never a delegated child or a shared MRU."""
    try:
        from agent.delegation_context import is_delegated_child_context
        if is_delegated_child_context() or not session_id:
            return
        path = Path(os.environ['AGENTSTACK_NATIVE_SESSION_FILE'])
        value = {'harness': 'hermes', 'native_session_id': session_id,
                 'created_at': datetime.now(timezone.utc).isoformat()}
        temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
        try:
            with temporary.open('w', encoding='utf-8') as handle:
                os.chmod(temporary, 0o600)
                json.dump(value, handle)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        if os.environ.get('AGENTSTACK_RUNTIME_MODE') == 'fresh' or 'AGENTSTACK_FRESH_CLIENT_CONTEXT' in os.environ:
            decision = call_hook('check-agent-registered.sh', dict(hook_event_name='SessionStart',
                session_id=session_id, cwd=os.getcwd()))
            if not decision:raise RuntimeError('FRESH_NATIVE_SESSION_UNAVAILABLE')
    except Exception:
        if os.environ.get('AGENTSTACK_RUNTIME_MODE') == 'fresh' or 'AGENTSTACK_FRESH_CLIENT_CONTEXT' in os.environ:
            raise RuntimeError('FRESH_NATIVE_SESSION_UNAVAILABLE') from None
        # Resume metadata is optional; failure must not prevent native startup.
        print('ORRERY could not save the native session reference', file=sys.stderr)


def register(ctx):
    ctx.register_hook('pre_tool_call', before)
    ctx.register_hook('post_tool_call', after)
    ctx.register_hook('on_session_start', session_started)
