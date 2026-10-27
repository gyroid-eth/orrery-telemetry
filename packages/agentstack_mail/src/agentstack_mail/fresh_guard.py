"""Fresh pre-edit hook selected only by a trusted, explicit caller context.

Codex keeps its existing patch parser and invokes the shared shell hook for
each source/destination. This module consumes the resulting host hook event,
not a model business tool. It never edits or grants a new reservation.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from .fresh_client import FreshBoundClient
from .storage_origin_contract import B1Error


async def check(context, payload):
    if not isinstance(payload, dict) or payload.get('tool_name') not in {'Edit', 'Write', 'Delete', 'MoveSource'}:
        raise B1Error('FRESH_HOOK_EVENT_INVALID')
    # Both values come from the host. Business tool_input cannot select cwd.
    observed = Path.cwd()
    cwd = payload.get('cwd')
    if cwd is not None and (not isinstance(cwd, str) or not Path(cwd).is_absolute()
                            or Path(cwd).resolve() != observed.resolve()):
        raise B1Error('FRESH_HOOK_CWD_MISMATCH')
    inputs = payload.get('tool_input')
    if not isinstance(inputs, dict):
        raise B1Error('FRESH_HOOK_EVENT_INVALID')
    value = inputs.get('file_path', inputs.get('path'))
    if not isinstance(value, str) or not value or '\x00' in value or len(value) > 4096:
        raise B1Error('INVALID_PATH')
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = observed / candidate
    if payload['tool_name'] in {'Delete', 'MoveSource'} and candidate.is_symlink():
        # Canonical target coverage cannot authorize removing the link entry.
        raise B1Error('RESERVATION_ENTRY_UNSUPPORTED')
    client = FreshBoundClient(context)
    result = await client.call('guard_edit', {'paths': [str(candidate)], 'extend_seconds': 1800})
    expected_paths = client._business('guard_edit', {'paths': [str(candidate)]})['paths']
    if (not isinstance(result, dict) or result.get('mail_instance_id') != client.context['mail_instance_id']
            or result.get('actor_agent_id') != client.context['mail_agent_id']
            or result.get('covered') is not True or not isinstance(result.get('items'), list)
            or [item.get('path') for item in result['items'] if isinstance(item, dict)] != expected_paths
            or any(not isinstance(item, dict) or type(item.get('reservation_id')) is not int
                   or item['reservation_id'] < 1 for item in result['items'])):
        raise B1Error('FRESH_GUARD_RESPONSE_INVALID')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True)
    options = parser.parse_args(argv)
    try:
        raw = sys.stdin.read(1048577)
        if len(raw) > 1048576:
            raise B1Error('FRESH_HOOK_EVENT_INVALID')
        asyncio.run(check(options.context, json.loads(raw)))
        return 0
    except B1Error as error:
        print('FRESH RESERVATION GUARD: ' + error.code, file=sys.stderr)
    except (ValueError, TypeError, OSError, RuntimeError):
        print('FRESH RESERVATION GUARD: FRESH_HOOK_UNAVAILABLE', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
