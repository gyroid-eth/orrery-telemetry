"""Explicit persistent profiles dispatched through the ordinary fresh launch."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import runpy
import signal
import subprocess
import sys
import time

from .fresh_runtime import _read_private, _write_private
from .storage_origin_contract import B1Error

KIND = 'orrery-fresh-persistent-v1'
SECRET_ENV = re.compile(r'(?:TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY)', re.I)


def profile(path, wrapper_root=None):
    value = _read_private(Path(path))
    if (set(value) - {'kind', 'context', 'provider', 'working_directory', 'channels', 'model', 'environment', 'managed', 'telegram'}
            or value.get('kind') != KIND or value.get('provider') not in {'claude', 'hermes'}):
        raise B1Error('FRESH_PERSISTENT_PROFILE_INVALID')
    for key in ('context', 'working_directory'):
        if not isinstance(value.get(key), str) or not Path(value[key]).is_absolute():
            raise B1Error('FRESH_PERSISTENT_PROFILE_INVALID')
    if value['provider'] == 'hermes':
        if (wrapper_root is None or not isinstance(value.get('managed'), dict)
                or not isinstance(value.get('model'), str) or not value['model']
                or 'channels' in value or 'environment' in value):
            raise B1Error('FRESH_PERSISTENT_PROFILE_INVALID')
        try:
            settings = runpy.run_path(str(Path(wrapper_root) / 'integrations/hermes/telegram/settings.py'))
            settings['validate'](value.get('telegram'))
        except (ValueError, TypeError):
            raise B1Error('FRESH_PERSISTENT_TELEGRAM_INVALID') from None
        return value
    if 'managed' in value or 'telegram' in value:
        raise B1Error('FRESH_PERSISTENT_PROFILE_INVALID')
    channels = value.get('channels')
    if (not isinstance(channels, list) or not 1 <= len(channels) <= 8
            or any(not isinstance(v, str) or not re.fullmatch(r'plugin:[A-Za-z0-9_.@/-]+', v) for v in channels)):
        raise B1Error('FRESH_PERSISTENT_CHANNELS_INVALID')
    model = value.get('model')
    if model is not None and (not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.\[\]-]{0,127}', model)):
        raise B1Error('FRESH_PERSISTENT_PROFILE_INVALID')
    env = value.get('environment', {})
    if (not isinstance(env, dict) or set(env) - {'TELEGRAM_STATE_DIR', 'SEMANTIC_SEARCH_DISABLED'}
            or 'TELEGRAM_STATE_DIR' in env and (not isinstance(env['TELEGRAM_STATE_DIR'], str)
                or not Path(env['TELEGRAM_STATE_DIR']).is_absolute() or '\0' in env['TELEGRAM_STATE_DIR'])
            or 'SEMANTIC_SEARCH_DISABLED' in env and (not isinstance(env['SEMANTIC_SEARCH_DISABLED'], str)
                or env['SEMANTIC_SEARCH_DISABLED'] not in {'0', '1'})):
        raise B1Error('FRESH_PERSISTENT_ENV_INVALID')
    return value


def claude_command(api, client, root, home, binary, run, cwd, options):
    args = [binary, '--channels', *options['channels']]
    if options.get('model'):args += ['--model', options['model']]
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(home))
    try:
        aliases = api['_claude_overlay_names'](args, cwd, env, {'mcp_url': client.context['endpoint']})
    except api['PersistentError']:
        raise B1Error('FRESH_CHANNELS_CONFIG_UNSUPPORTED') from None
    proxy = {'command': '/bin/bash', 'args': [str(root / 'bin/agentstack-runtime-client'), 'proxy'],
             'env': {'AGENTSTACK_RUNTIME_MODE': 'fresh', 'AGENTSTACK_FRESH_CLIENT_CONTEXT': str(client.path),
                     'AGENTSTACK_PYTHON': sys.executable}}
    _write_private(run / 'mcp.json', {'mcpServers': {name: proxy for name in aliases | {'orrery-mail'}}})
    return [*args, '--mcp-config', str(run / 'mcp.json')]


def request_restart(client, payload):
    """A changed session stops this run; it never replaces its receipt."""
    from .fresh_launch import layout, native_id, pinned
    base = layout(client)
    run = base / 'runs' / native_id(os.environ.get('AGENTSTACK_FRESH_LAUNCH_ID'))
    intent = _read_private(run / 'intent.json');pinned(client, intent)
    if (not intent.get('persistent') or intent.get('nonce') != os.environ.get('AGENTSTACK_FRESH_LAUNCH_NONCE')
            or _read_private(base / 'current.json') != {'launch_id': run.name}
            or (run / 'ended.json').exists()):return
    saved = _read_private(run / ('session.json' if (run / 'session.json').exists() else 'session-start.json'))
    pinned(client, saved)
    if saved.get('native_session_id') != payload.get('session_id'):
        path = run / 'restart-required.json'
        value = {'launch_id': run.name, 'reason': 'FRESH_BOT_RESTART_REQUIRED'}
        try:_write_private(path, value)
        except FileExistsError:
            if _read_private(path) != value:raise B1Error('FRESH_LAUNCH_RECEIPT_MISMATCH')
        raise B1Error('FRESH_BOT_RESTART_REQUIRED')


def wait_provider(process, run, *, wrapper_root=None):
    """Observe this bot's children through normal exit as well as requested stop."""
    observation = (runpy.run_path(str(Path(wrapper_root) / 'bin/lib/process_observation.py'))
                   if wrapper_root is not None else None)
    descendants = {}
    # The owned foreground pane/job supplies one process group for this run.
    # It also identifies a short-lived wrapper's reparented TUI children.
    group = os.getpid()
    if observation is not None and os.getpgrp() != group:
        raise B1Error('FRESH_LAUNCH_OUTCOME_UNKNOWN')

    def remaining():
        if observation is None:return {}
        rows = observation['process_snapshot'](include_groups=True)
        if rows is None:raise B1Error('FRESH_LAUNCH_OUTCOME_UNKNOWN')
        for pid, row in rows.items():
            if pid in {process.pid, os.getpid()} or row[1] != os.getuid():continue
            # The observer's ps is a direct child of this launcher, not the TUI.
            grouped = row[3] == group and row[0] != os.getpid()
            if grouped or observation['snapshot_belongs_to'](rows, pid, process.pid, os.getuid()):
                descendants[pid] = row[2]
        return {pid: birth for pid, birth in descendants.items()
                if pid in rows and rows[pid][1:3] == (os.getuid(), birth)}

    previous = {}
    stopping = None
    requested_signal = None
    restarted = False

    def stop(signum, _frame):
        nonlocal stopping, requested_signal
        if stopping is None:
            stopping = time.monotonic()
            for pid in remaining():
                try:os.kill(pid, signum)
                except ProcessLookupError:pass
            try:
                process.send_signal(signum)
                requested_signal = signum
            except ProcessLookupError:pass

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, stop)
        while True:
            alive = process.poll() is None
            children = remaining()
            if not alive:
                if not children:break
                if stopping is None:raise B1Error('FRESH_LAUNCH_OUTCOME_UNKNOWN')
            if (run / 'restart-required.json').exists() and stopping is None:
                restarted = True
                stop(signal.SIGTERM, None)
            if stopping is not None and time.monotonic() - stopping > 15:
                for pid in children:
                    try:os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:pass
                if process.poll() is None:process.kill()
                process.wait()
                raise B1Error('FRESH_LAUNCH_OUTCOME_UNKNOWN')
            if not alive:time.sleep(0.1)
            else:
                try:process.wait(timeout=1 if observation is not None else 0.1)
                except subprocess.TimeoutExpired:pass
        code = process.wait()
        if code != 0 and (requested_signal is None or code not in {-requested_signal, 128 + requested_signal}):
            raise B1Error('FRESH_LAUNCH_OUTCOME_UNKNOWN')
        return code, restarted or (run / 'restart-required.json').exists()
    finally:
        for sig, handler in previous.items():signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('run', 'inspect'))
    parser.add_argument('--profile', required=True)
    parser.add_argument('--wrapper-root', required=True)
    args = parser.parse_args(argv)
    try:
        from .fresh_client import FreshBoundClient
        from .fresh_launch import launch
        from .fresh_proxy import verified_owner
        value = profile(args.profile, args.wrapper_root)
        client = FreshBoundClient(value['context'])
        if (os.environ.get('AGENTSTACK_CLIENT_CONFIG') or os.environ.get('AGENTSTACK_GLOBAL_SERVER_CONFIG')
                or os.environ.get('AGENTSTACK_FRESH_CLIENT_CONTEXT', str(client.path)) != str(client.path)):
            raise B1Error('FRESH_CONTEXT_CONFLICT')
        owner = asyncio.run(verified_owner(client))
        if args.operation == 'inspect':
            print(json.dumps({'ok': True, 'kind': KIND, 'provider': value['provider'], **owner}))
            return 0
        return launch(client, args.wrapper_root, value['provider'], value['working_directory'], persistent=value)
    except (B1Error, OSError, ValueError, TypeError) as exc:
        reason = exc.code if isinstance(exc, B1Error) else 'FRESH_PERSISTENT_PROFILE_INVALID'
        print('FRESH PERSISTENT: ' + reason, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
