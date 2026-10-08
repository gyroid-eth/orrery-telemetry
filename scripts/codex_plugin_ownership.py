"""Current registry and service ownership checks for the dedicated lifecycle."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


class OwnershipUnknown(RuntimeError):
    pass


def cli_json(binary, home, arguments):
    response = subprocess.run([binary, *arguments],
                              env=dict(os.environ, CODEX_HOME=str(home)),
                              capture_output=True, text=True, check=True, timeout=20)
    value = json.loads(response.stdout)
    if not isinstance(value, dict):
        raise OwnershipUnknown('registry response is not an object')
    return value


def registry_snapshot(binary, home):
    plugins = cli_json(binary, home, ['plugin', 'list', '--json']).get('installed')
    markets = cli_json(binary, home, ['plugin', 'marketplace', 'list', '--json']).get('marketplaces')
    if not isinstance(plugins, list) or not isinstance(markets, list):
        raise OwnershipUnknown('current registry is unavailable')
    if any(not isinstance(item, dict) for item in plugins + markets):
        raise OwnershipUnknown('current registry entries are invalid')
    return plugins, markets


def check_registry(snapshot, plugin_id, name, root):
    """Return current presence after validating both independent registrations."""
    if not root or not Path(root).is_absolute():
        raise OwnershipUnknown('saved marketplace ownership is unavailable')
    root = Path(root).resolve()
    plugins, markets = snapshot
    selected = [p for p in plugins if p.get('pluginId') == plugin_id]
    market = [m for m in markets if m.get('name') == name]
    if len(selected) > 1 or len(market) > 1:
        raise OwnershipUnknown('registry ownership is ambiguous')
    if selected and not market:
        raise OwnershipUnknown('installed plugin has no corresponding marketplace')
    if market:
        source = market[0].get('marketplaceSource', {})
        if (source.get('sourceType') != 'local' or not source.get('source')
                or Path(source['source']).resolve() != root
                or not market[0].get('root') or Path(market[0]['root']).resolve() != root):
            raise OwnershipUnknown('current marketplace differs from its receipt')
    if selected:
        item = selected[0]
        source, market_source = item.get('source', {}), item.get('marketplaceSource', {})
        if (item.get('marketplaceName') != name or market_source.get('sourceType') != 'local'
                or not market_source.get('source') or Path(market_source['source']).resolve() != root
                or source.get('source') != 'local' or not source.get('path')
                or Path(source['path']).resolve() != root/'plugins/agentstack-codex-app'):
            raise OwnershipUnknown('current plugin differs from its receipt')
    in_use = any(p.get('pluginId') != plugin_id and p.get('marketplaceName') == name for p in plugins)
    return bool(selected), bool(market), in_use


def linux_start_token(pid, proc_root=Path('/proc')):
    """Kernel start ticks and boot identity, independent of wall-clock display."""
    process = proc_root / str(pid)
    try:
        raw = (process / 'stat').read_text()
        boot = (proc_root / 'sys/kernel/random/boot_id').read_text().strip()
    except FileNotFoundError:
        if not process.exists():
            return None
        raise OwnershipUnknown('kernel process identity is unavailable')
    # comm may contain spaces or closing parentheses; fields after its final
    # closing parenthesis begin with field 3 (state), starttime is field 22.
    head, separator, tail = raw.rpartition(')')
    fields = tail.split()
    if not separator or not head.startswith(str(pid)+' (') or len(fields) < 20 or not boot or not fields[19].isdigit():
        raise OwnershipUnknown('kernel process identity is incomplete')
    if fields[0] == 'Z':
        return None
    return 'linux:'+boot+':'+fields[19]


def process_identity(pid):
    """Unknown inspection is different from an observed missing/zombie process."""
    linux = sys.platform.startswith('linux')
    before = linux_start_token(pid) if linux else None
    if linux and before is None:
        return None
    result = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'uid=', '-o', 'lstart=',
                             '-o', 'stat=', '-o', 'command='],
                            env=dict(os.environ, LC_ALL='C'),
                            capture_output=True, text=True, timeout=2)
    if result.returncode:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        raise OwnershipUnknown('process inspection is unavailable')
    fields = result.stdout.strip().split(None, 7)
    if len(fields) != 8:
        raise OwnershipUnknown('process inspection is incomplete')
    if fields[6].startswith('Z'):
        return None
    start = ' '.join(fields[1:6])
    if linux:
        after = linux_start_token(pid)
        if after is None:
            return None
        if after != before:
            raise OwnershipUnknown('process changed during inspection')
        start = after
    return {'pid': pid, 'uid': int(fields[0]), 'start': start,
            'command': fields[7]}


def check_process(saved, runner):
    if not isinstance(saved, dict) or not isinstance(saved.get('pid'), int) or saved['pid'] <= 1:
        raise OwnershipUnknown('saved process identity is unavailable')
    command = saved.get('command', '')
    prefix = '/bin/bash '
    if (saved.get('uid') != os.getuid() or not command.startswith(prefix)
            or Path(command[len(prefix):]).resolve() != Path(runner).resolve()
            or not saved.get('start')):
        raise OwnershipUnknown('saved process does not belong to this integration')
    current = process_identity(saved['pid'])
    if current is not None and current != {k: saved[k] for k in ('pid', 'uid', 'start', 'command')}:
        changed = ','.join(k for k in ('pid', 'uid', 'start', 'command') if current.get(k) != saved.get(k))
        raise OwnershipUnknown('current process differs from its receipt (changed: '+changed+')')
    return current


def capture_process(pidfile, runner):
    pid = int(Path(pidfile).read_text().splitlines()[0])
    identity = process_identity(pid)
    if identity is None:
        raise OwnershipUnknown('supervisor is not running')
    check_process(identity, runner)
    return identity


def record_process(pidfile, runner):
    """Wait for the expected runner after exec; never persist a transient parent."""
    from codex_plugin_trust import atomic_json
    for attempt in range(20):
        try:
            identity = capture_process(pidfile, runner)
            atomic_json(Path(str(pidfile)+'.identity.json'), identity)
            return identity
        except OwnershipUnknown:
            if attempt == 19:
                raise
            time.sleep(.05)


def preflight_supervisor(pidfile, runner, saved=None):
    path = Path(pidfile)
    if saved is None:
        identity_path = Path(str(path)+'.identity.json')
        if identity_path.exists():
            saved = json.loads(identity_path.read_text())
    if saved is not None:
        if path.exists() and int(path.read_text().splitlines()[0]) != saved.get('pid'):
            raise OwnershipUnknown('pidfile differs from its receipt')
        return saved if check_process(saved, runner) is not None else None
    if not path.exists():
        return None
    pid = int(path.read_text().splitlines()[0])
    if pid <= 1 or process_identity(pid) is not None:
        raise OwnershipUnknown('legacy pidfile has no verified process receipt')
    return None


def stop_supervisor(pidfile, runner, saved=None):
    owned = preflight_supervisor(pidfile, runner, saved)
    if owned is not None and check_process(owned, runner) is not None:
        os.kill(owned['pid'], signal.SIGTERM)
        deadline = time.monotonic()+5
        while time.monotonic() < deadline and check_process(owned, runner) is not None:
            time.sleep(.05)
        if check_process(owned, runner) is not None:
            os.kill(owned['pid'], signal.SIGKILL)
            deadline = time.monotonic()+2
            while time.monotonic() < deadline and check_process(owned, runner) is not None:
                time.sleep(.05)
            if check_process(owned, runner) is not None:
                raise OwnershipUnknown('service exit was not observed')
    Path(pidfile).unlink(missing_ok=True)
    Path(str(pidfile)+'.identity.json').unlink(missing_ok=True)


def check_launchd(data, install_dir):
    """Check both the owned plist and the currently loaded job before bootout."""
    import plistlib
    import re
    path = Path(data['path']).resolve()
    if path.exists():
        if path.stat().st_uid != os.getuid():
            raise OwnershipUnknown('launchd plist belongs to another user')
        definition = plistlib.loads(path.read_bytes())
        if definition.get('Label') != data['label'] or not owned_arguments(definition.get('ProgramArguments'), install_dir):
            raise OwnershipUnknown('launchd plist differs from its receipt')
    job = f"gui/{os.getuid()}/{data['label']}"
    result = subprocess.run(['launchctl', 'print', job], capture_output=True, text=True, timeout=3)
    if result.returncode:
        if 'Could not find service' in result.stderr:
            return False
        raise OwnershipUnknown('launchd inspection is unavailable')
    match = re.search(r'\n\s*arguments = \{\n(.*?)\n\s*\}', result.stdout, re.S)
    arguments = [line.strip() for line in match[1].splitlines()] if match else []
    if not owned_arguments(arguments, install_dir):
        raise OwnershipUnknown('loaded launchd definition differs from its receipt')
    return True


def owned_arguments(arguments, install_dir):
    return (isinstance(arguments, list) and len(arguments) == 2 and arguments[0] == '/bin/bash'
            and Path(arguments[1]).resolve() == (Path(install_dir)/'bin/run-bridge').resolve())


def main():
    import argparse
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['record', 'stop'])
    parser.add_argument('--pidfile', required=True)
    parser.add_argument('--runner', required=True)
    args = parser.parse_args()
    try:
        if args.operation == 'record':
            record_process(args.pidfile, args.runner)
        else:
            stop_supervisor(args.pidfile, args.runner)
        return 0
    except (OwnershipUnknown, OSError, ValueError, subprocess.SubprocessError) as exc:
        print('service ownership could not be verified; retained its files: '+str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
