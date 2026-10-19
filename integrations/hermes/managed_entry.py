"""Load and verify the reservation callback in the inference process itself."""
import argparse
import functools
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mail_lifecycle import read, save, stamp
from runner import check_configured, digest, pilot_toolsets
from task_evidence import audit, callers, event


def deny_dotenv(state):
    def guard(name, args):
        if name == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
            if Path(os.fsdecode(args[0])).name in ('.env', '.op.env'):
                audit(state, 'blocked-dotenv', reason='managed-profile-uses-key-command-references',
                      method='open', host=None, port=None, path=os.fsdecode(args[0]), callers=callers(state))
                raise PermissionError('Managed entry forbids dotenv file reads and writes')
    sys.addaudithook(guard)


def skip_absent_dotenv(state):
    """Keep Hermes's missing-file result without attempting a forbidden open.

    Existing files and dangling symlinks still reach the denying audit hook.
    No installed source or live configuration is changed.
    """
    from agent import secret_scope
    original = secret_scope.load_env_file

    @functools.wraps(original)
    def load(path):
        path = Path(path)
        if path.name in ('.env', '.op.env') and not path.exists() and not path.is_symlink():
            secret_scope.invalidate_env_file_cache(path)
            audit(state, 'dotenv-absent', method='stat', host=None, port=None,
                  path=str(path), callers=callers(state))
            return {}
        return original(path)
    secret_scope.load_env_file = load


def load_guard(state):
    state = Path(state).resolve()
    config, identity = check_configured(state)
    profile = Path(config['profile_home'])
    if os.environ.get('HERMES_HOME') != str(profile) or config['purpose'] != 'file-task':
        raise ValueError('Explicit current managed profile required')
    for path in (profile / '.env', profile / '.op.env', Path(config['hermes_source']) / '.env'):
        if path.exists() or path.is_symlink():
            raise ValueError('Managed profile or source dotenv exists')
    deny_dotenv(state)
    sys.path.insert(0, config['hermes_source'])
    skip_absent_dotenv(state)
    from hermes_cli import plugins
    from task_plugin import assert_registered
    plugins.discover_plugins()
    assert_registered(plugins.get_plugin_manager(), state)
    from toolsets import create_custom_toolset
    create_custom_toolset('orrery_file', 'Reserved task files', tools=['read_file', 'write_file'])
    task = read(state / 'task.json')
    from reservation_guard import create_guard
    guard = create_guard(read(state / 'attempt.json'), task, lambda row: None)
    for spec in task['expected']:
        if guard('write_file', {'path': str(Path(task['worktree']) / spec['path'])}):
            raise ValueError('Current task reservation is not usable')
    return config, identity


def turn_writer(state):
    def write(_path, result, failure=None):
        # Hermes's original writer can include raw exception text. Save only
        # completion flags and an explicit source binding, never that text.
        attempt, task = read(Path(state) / 'attempt.json'), read(Path(state) / 'task.json')
        completed = (result.get('completed') is True and not result.get('failed')
                     and not result.get('partial') and not result.get('interrupted') and failure is None)
        reason = 'completed' if completed else 'interrupted' if result.get('interrupted') else 'failed'
        save(Path(state) / 'hermes-turn.json', {'attempt_id': attempt['attempt_id'], 'task_id': task['task_id'],
             'completed': completed, 'reason_kind': reason, 'source': 'hermes-oneshot-result', 'at': stamp()})
        event(state, 'turn-end', reason_kind=reason)
    return write


def execute(state, check=False):
    config, identity = load_guard(state)
    if check:
        proof = {'attempt_id': read(Path(state) / 'attempt.json')['attempt_id'],
                 'agent_name': identity['agent_name'], 'guard_loaded': True,
                 'task_sha256': digest(Path(state) / 'task.json'),
                 'profile_sha256': digest(Path(config['profile_home']) / 'config.yaml'),
                 'source': 'installed-plugin-manager', 'checked_at': stamp()}
        save(Path(state) / 'preflight-complete.json', proof)
        return 0
    from provider_audit import install
    install(state)
    # The same manager/callback survives profile resolution: HERMES_HOME already
    # pins this exact profile. No separate preflight marker substitutes for it.
    profile = read(Path(config['profile_home']) / 'config.yaml')
    sys.argv = [config['hermes_executable'], '-p', config['profile_name'], '-z',
                (Path(state) / 'task.md').read_text(), '--provider', config['provider'], '-m', config['model'],
                '-t', ','.join(pilot_toolsets(profile, file_task=True)),
                '--usage-file', str(Path(state) / 'hermes-turn.json')]
    from hermes_cli.main import main
    from hermes_cli import oneshot
    from task_plugin import assert_registered
    from hermes_cli import plugins
    assert_registered(plugins.get_plugin_manager(), state)
    oneshot._write_usage_file = turn_writer(state)
    return main()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        return execute(args.state, args.check)
    except BaseException as error:
        if isinstance(error, SystemExit):
            return error.code if type(error.code) is int else 1
        audit(args.state, 'runtime-fatal', reason=type(error).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
