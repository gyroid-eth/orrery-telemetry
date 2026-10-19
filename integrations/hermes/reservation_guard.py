"""Offline spike: map Hermes pre_tool_call onto the shared receipt contract.

Not a launcher or a sandbox. The caller must verify registration before inference,
disable argument-rewriting plugins, and expose only these two file tools.
"""
from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
from pathlib import Path, PurePosixPath


def _matches(relative, pattern):
    # Deliberately small subset: exact paths and subtree/**; unsupported globs deny.
    if not isinstance(pattern, str):
        return False
    parts = PurePosixPath(pattern).parts
    if pattern.startswith('/') or '..' in parts:
        return False
    if pattern.endswith('/**') and not any(c in pattern[:-3] for c in '*?['):
        return relative.startswith(pattern[:-2])
    return not any(c in pattern for c in '*?[') and relative == pattern


def create_guard(attempt, task, emit, now=None):
    """Return a synchronous veto callback. Failures produce a block directive."""
    now = now or (lambda: datetime.now(timezone.utc).timestamp())
    attempt, task = deepcopy(attempt), deepcopy(task)
    root = Path(task['worktree']).resolve(strict=True)
    if attempt['attempt_id'] != task['attempt_id'] or attempt['phase'] not in ('spike', 'run'):
        raise ValueError('manifest mismatch')
    if attempt['phase'] == 'run' and task.get('reservation_source') == 'offline-fixture':
        raise ValueError('fixture receipt in run')

    def before(tool_name, args, **_):
        reason, relative = None, None
        try:
            if tool_name not in ('read_file', 'write_file'):
                reason = 'not-in-wire-tools'
            else:
                supplied = args.get('path')
                if not isinstance(supplied, str) or not Path(supplied).is_absolute():
                    reason = 'outside-worktree'
                else:
                    target = Path(supplied).resolve()
                    if not target.is_relative_to(root) or '.git' in target.relative_to(root).parts:
                        reason = 'outside-worktree'
                    else:
                        relative = target.relative_to(root).as_posix()
                        receipts = [r for r in task.get('reservation_receipts', [])
                                    if type(r.get('id')) is int and r['id'] > 0
                                    and r.get('owner') == attempt['agent_name']
                                    and _matches(relative, r.get('path_pattern'))]
                        if not any(_matches(relative, p) for p in task.get('allowed_paths', [])) or not receipts:
                            reason = 'not-reserved'
                        elif not any(datetime.fromisoformat(r['expires_ts'].replace('Z', '+00:00')).timestamp()
                                     > now() + 1 for r in receipts):
                            reason = 'reservation-expired'
            emit({'attempt_id': attempt['attempt_id'], 'task_id': task['task_id'],
                  'phase': 'tool-denied' if reason else 'tool-allowed',
                  'tool': tool_name, 'path': relative, **({'reason': reason} if reason else {})})
            return {'action': 'block', 'message': reason} if reason else None
        except Exception:
            return {'action': 'block', 'message': 'reservation-guard-error'}

    return before


def require_guard(manager, callback):
    """Managed startup must stop before inference when the guard was skipped."""
    if callback not in manager.iter_hook_callbacks('pre_tool_call'):
        raise RuntimeError('required reservation guard is not registered')
