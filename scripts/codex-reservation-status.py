#!/usr/bin/env python3
"""Read-only diagnostic for the current Codex plugin's reservation hook trust."""
from __future__ import annotations

import argparse
from pathlib import Path

from codex_plugin_trust import AppServer, TrustUnavailable


def status(server, cwd: Path, plugin_id: str) -> str:
    result = server.request('hooks/list', {'cwds': [str(cwd)]})
    entries = [item for item in result.get('data', []) if item.get('cwd') == str(cwd)]
    if len(entries) != 1 or entries[0].get('errors'):
        return 'unknown'
    matches = [h for h in entries[0].get('hooks', [])
               if h.get('pluginId') == plugin_id
               and h.get('eventName') in ('preToolUse', 'PreToolUse', 'pre_tool_use')
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-binary', required=True)
    parser.add_argument('--codex-home', type=Path, required=True)
    parser.add_argument('--cwd', type=Path, required=True)
    parser.add_argument('--plugin-id', required=True)
    args = parser.parse_args()
    try:
        with AppServer(args.codex_binary, args.codex_home, args.cwd, timeout=5) as server:
            state = status(server, args.cwd, args.plugin_id)
    except (TrustUnavailable, OSError, ValueError, TypeError, AttributeError):
        state = 'unknown'
    if state == 'trusted':
        print('ok: Codex apply_patch reservation guard is enabled and its current hook definition is trusted')
    else:
        print(f'warn: Codex apply_patch reservation guard is {state}; edits may be unguarded. '
              'Refresh the core installation and plugin; review /hooks, then start a new Codex process.')


if __name__ == '__main__':
    main()
