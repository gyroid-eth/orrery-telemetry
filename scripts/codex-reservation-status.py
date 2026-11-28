#!/usr/bin/env python3
"""Read-only diagnostic for the current ORRERY Codex plugin's hook trust."""
from __future__ import annotations

import argparse
from pathlib import Path

from codex_plugin_trust import AppServer, TrustUnavailable


def plugin_hooks(server, cwd: Path, plugin_id: str | None) -> list[dict] | None:
    result = server.request('hooks/list', {'cwds': [str(cwd)]})
    entries = [item for item in result.get('data', []) if item.get('cwd') == str(cwd)]
    if len(entries) != 1 or entries[0].get('errors'):
        return None
    hooks = entries[0].get('hooks')
    if not isinstance(hooks, list) or any(not isinstance(h, dict) for h in hooks):
        return None
    if plugin_id is None:
        return [h for h in hooks if isinstance(h.get('pluginId'), str)
                and h['pluginId'].startswith('agentstack-codex-app@')]
    return [h for h in hooks if h.get('pluginId') == plugin_id]


def reservation_status(hooks: list[dict] | None) -> str:
    if hooks is None:
        return 'unknown'
    matches = [h for h in hooks
               if h.get('eventName') in ('preToolUse', 'PreToolUse', 'pre_tool_use')
               and h.get('matcher') == '^apply_patch$'
               and 'run-reservation-guard.sh' in str(h.get('command', h.get('handler', {}).get('command', '')))]
    if not matches:
        return 'missing'
    if len(matches) != 1:
        return 'unknown'
    hook = matches[0]
    source = hook.get('sourcePath')
    if not isinstance(source, str) or not Path(source).is_absolute():
        return 'unknown'
    runner = Path(source).parent.parent / 'scripts/run-reservation-guard.sh'
    handler = hook.get('handler') or {'type': hook.get('handlerType'),
                                      'command': hook.get('command'), 'async': hook.get('async', False)}
    if (hook.get('source') != 'plugin' or hook.get('isManaged') is not False
            or handler.get('type') != 'command' or handler.get('async', False) is not False
            or handler.get('command') not in {'"$PLUGIN_ROOT/scripts/run-reservation-guard.sh"',
                                               '"' + str(runner) + '"'}):
        return 'unknown'
    if hook.get('enabled') is not True:
        return 'disabled'
    return 'trusted' if hook.get('trustStatus') == 'trusted' else 'needs_review'


def lifecycle_report(hooks: list[dict] | None) -> str:
    states = [(h.get('eventName'), h.get('trustStatus')) for h in hooks or []
              if h.get('source') == 'plugin']
    review = [f'{event} ({state})' for event, state in states
              if isinstance(event, str) and event.isidentifier() and state in ('modified', 'untrusted')]
    if review:
        return f'warn: {len(review)} ORRERY Codex hooks need review in /hooks — ' + ', '.join(review)
    if hooks and len(states) == len(hooks) and all(state == 'trusted' for _, state in states):
        return f'ok: All {len(hooks)} discovered ORRERY Codex hooks are trusted'
    return 'note: Codex lifecycle hook approval is unknown. Inspect the ORRERY hooks in /hooks.'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-binary', required=True)
    parser.add_argument('--codex-home', type=Path, required=True)
    parser.add_argument('--cwd', type=Path, required=True)
    parser.add_argument('--plugin-id')
    parser.add_argument('--lifecycle-only', action='store_true')
    args = parser.parse_args()
    if args.plugin_id is None and not args.lifecycle_only:
        parser.error('--plugin-id is required for the reservation guard diagnostic')
    try:
        with AppServer(args.codex_binary, args.codex_home, args.cwd, timeout=5) as server:
            hooks = plugin_hooks(server, args.cwd, args.plugin_id)
    except (TrustUnavailable, OSError, ValueError, TypeError, AttributeError):
        hooks = None
    if args.lifecycle_only and hooks == []:
        return
    print(lifecycle_report(hooks))
    if args.lifecycle_only:
        return
    state = reservation_status(hooks)
    if state == 'trusted':
        print('ok: Codex apply_patch reservation guard is enabled and its current hook definition is trusted')
    else:
        print(f'warn: Codex apply_patch reservation guard is {state}; edits may be unguarded. '
              'Refresh the core installation and plugin; review /hooks, then start a new Codex process.')


if __name__ == '__main__':
    main()
