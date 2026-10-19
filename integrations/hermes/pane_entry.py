"""Owned pane supervisor for the Mail pilot. No credentials are read here."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mail_lifecycle import read, save, stamp
from runner import base_env, check_configured


def group_absent(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return None
    return False


def terminate(child):
    for sig, timeout in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            break
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            continue
        if group_absent(child.pid) is True:
            break


def execute(state):
    state = Path(state).resolve()
    config, identity = check_configured(state)
    if read(state / 'start-intent.json')['agent_name'] != identity['agent_name']:
        raise ValueError('Owned pane identity mismatch')
    profile = Path(config['profile_home'])
    env = base_env(config) | {'HERMES_HOME': str(Path(config['managed_root'])),
          'ORRERY_HERMES_STATE_ROOT': str(state), 'ORRERY_HERMES_ADAPTER_DIR': str(Path(__file__).resolve().parent),
          'AGENT_NAME': identity['agent_name'], 'PARENT_AGENT': config['parent']}
    # The selected profile's key_cmd is resolved by Hermes in this owned pane.
    # No inherited API key, token value, shell config, or another profile's .env.
    command = [config['hermes_executable'], '-p', config['profile_name'], '-z', (state / 'task.md').read_text(),
               '--provider', config['provider'], '-m', config['model'], '-t', 'mcp-orrery_mail,memory,skills,session_search']
    save(state / 'pane-entry.json', {'supervisor_pid': os.getpid(), 'agent_name': identity['agent_name'],
         'profile_home': str(profile), 'at': stamp()}, exclusive=True)
    child = None
    stopping = []

    def stop_handler(signum, _):
        stopping.append(signum)
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, stop_handler)
    code, reason = None, 'failure'
    try:
        with open(state / 'stdout.log', 'w') as out, open(state / 'stderr.log', 'w') as err:
            child = subprocess.Popen(command, cwd=config['worktree'], env=env,
                                     stdout=out, stderr=err, start_new_session=True)
            save(state / 'runtime-process.json', {'pid': child.pid, 'pgid': child.pid,
                 'agent_name': identity['agent_name'], 'started_at': stamp()}, exclusive=True)
            try:
                code = child.wait(timeout=config['task_timeout_seconds'])
                reason = 'parent' if stopping else 'task-done' if code == 0 else 'failure'
            except subprocess.TimeoutExpired:
                reason = 'limit'
                terminate(child)
                code = child.returncode
    finally:
        absent = None
        if child is not None:
            if child.poll() is None or group_absent(child.pid) is False:
                terminate(child)
            absent = group_absent(child.pid)
        save(state / 'runtime-exit.json', {'exit_code': code, 'signal': stopping[-1] if stopping else None,
             'group_absent': absent, 'group_check': 'observed' if absent is True else 'unknown',
             'stopped_by': reason, 'checked_at': stamp()})
    return code if isinstance(code, int) and code >= 0 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    args = parser.parse_args()
    try:
        return execute(args.state)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
