"""TEST-only real shell prerequisite and delivery-only task boundary checks."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path

from .caller_binding import read_binding_trace, assert_single_replacement
from .caller_resume import checked_saved_intent, set_fixture_binding


def prepare_shell(probe, data):
    owner = Path(data['child_context']).parent/'profile.json'
    saved = checked_saved_intent(owner, None)
    before = owner.read_bytes()
    task = owner.parent/'runtime/task.md'
    assert not task.exists() and not task.is_symlink(), 'shell borrowed an earlier task'
    trace_before = len(read_binding_trace(probe.trace))
    if not probe.enabled:
        # As with real resume, a valid prerequisite is separate from the
        # actual disabled CLI's fresh trace and cannot satisfy that refusal.
        set_fixture_binding(probe, True)
    try:
        result = probe.run_entry('preregister-shell', data)
    finally:
        if not probe.enabled:set_fixture_binding(probe, False)
    rows = read_binding_trace(probe.trace)[trace_before:]
    assert_single_replacement(rows, enabled=True)
    assert result.returncode == 0, 'R3_REAL_SHELL_PREREGISTER_REJECTED: '+result.stderr
    value = json.loads(result.stdout)
    assert value['prepared'] is True and value['runtime_ready'] is False
    assert value['client_config'] == data['child_context']
    calls = {(row.get('file'),row.get('name')) for row in rows if row['event']=='real-call'}
    assert (str(probe.install/'bin/lib/runtime_client.py'),'prepare_child') in calls
    assert owner.read_bytes() == before and checked_saved_intent(owner, saved) == saved
    assert not task.exists() and not task.is_symlink(), 'preregister performed shell task publication'
    probe.last_shell_prerequisite = {'rows':rows, 'intent':saved, 'owner_bytes':before}
    data['shell_intent_digest'] = saved['intent_digest']


def shell_argv(probe, data, options):
    caller = deepcopy(data['caller'])
    if 'shell_caller_overrides' in options:
        overrides = options['shell_caller_overrides']
        assert options.get('inspect_prepare_only') and isinstance(overrides,dict) and len(overrides)==1
        caller.update(overrides)
    required = {'provider','model','cwd','task','name','parent','tools','base',
                'clear_tools','detached','resources','worktree','chrome'}
    optional = {'effort','strict_output_cap','task_delivery'}
    assert required <= caller.keys() and not caller.keys()-required-optional, 'unrepresentable fixture caller fields'
    assert caller['provider'] in ('codex','claude'), 'unsupported fixture shell provider'
    assert isinstance(caller['tools'],dict) and set(caller['tools']) <= {'mcp'}, 'unsupported fixture CLI tools shape'
    assert isinstance(caller['tools'].get('mcp',[]),list)
    assert all(isinstance(value,str) and value for value in caller['tools'].get('mcp',[]))
    assert all(',' not in value for value in caller['resources']), 'unrepresentable fixture resource'
    argv = ['/bin/bash', str(probe.install/'hooks/spawn_child.sh'),
        '--child-client-name','child_01','--name',caller['name'],
        '--model',caller['model'],'--parent',caller['parent'],
        '--base',caller['base'],'--task-file',caller['task'],
        '--resources='+','.join(caller['resources']),'--prepare-only']
    argv += ['--codex'] if caller['provider']=='codex' else ['--program','claude-code']
    for key, flag in [('detached','detached'), ('clear_tools','clear-tools'),
                      ('worktree','worktree'), ('chrome','chrome')]:
        assert type(caller[key]) is bool
        argv.append('--'+('' if caller[key] else 'no-')+flag)
    for value in options.get('invalid_cli_tools',caller['tools'].get('mcp',[])):
        argv += ['--tools', value if 'invalid_cli_tools' in options else 'mcp:'+value]
    for key, flag in [('effort','effort'),('task_delivery','task-delivery')]:
        if key in caller:
            assert isinstance(caller[key],str), 'unrepresentable fixture optional field: '+key
            argv.append('--'+flag+'='+caller[key])
    if 'strict_output_cap' in caller:
        assert type(caller['strict_output_cap']) is bool
        argv.append('--'+('' if caller['strict_output_cap'] else 'no-')+'strict-output-cap')
    return [*argv, caller['cwd']]


@contextmanager
def shell_mutation(probe, options):
    """Arm only the actual CLI, never setup, preregister, or R2 children."""
    if not options.get('real_entry_mutation'):
        yield
        return
    assert options['real_entry_mutation'] == 'no-op'
    assert probe.enabled is True
    paths = list((probe.workspace.tmp/'delivery-python/lib').glob('python*/site-packages/delivery-fixture.json'))
    assert len(paths)==1
    path = paths[0]
    assert path.resolve()==path and path.stat().st_uid==os.getuid()
    assert path.stat().st_mode & 0o777 == 0o400
    original = path.read_bytes()
    config = json.loads(original)
    assert config['install']==str(probe.install) and 'shell_noop' not in config
    config['shell_noop'] = {'kind':'no-op', 'trace':str(probe.workspace.tmp/'mutation-shell.jsonl')}
    path.chmod(0o600)
    try:
        path.write_text(json.dumps(config,sort_keys=True))
        path.chmod(0o400)
        yield
    finally:
        path.chmod(0o600)
        path.write_bytes(original)
        path.chmod(0o400)
    assert path.read_bytes()==original


def check_shell_task(probe, data, result, rows):
    owner = Path(data['child_context']).parent/'profile.json'
    task = owner.parent/'runtime/task.md'
    # Check BEFORE parsing success-looking stdout or allowing a delivery helper.
    # A root entry no-op must fail here, not at unrelated JSON parsing or ps.
    if not task.is_file() or task.is_symlink():
        raise RuntimeError('R3_REAL_CALLER_REJECTED: shell: R3_REAL_SHELL_TASK_PREPARATION_MISSING')
    assert task.read_bytes()==Path(data['caller']['task']).read_bytes()
    previous = probe.last_shell_prerequisite
    assert owner.read_bytes()==previous['owner_bytes']
    intent = checked_saved_intent(owner, previous['intent'])
    prepared = json.loads(result.stdout)
    assert prepared['prepared'] is True and prepared['runtime_ready'] is False
    identity = json.loads(Path(data['child_context']).read_text())['identity']
    assert prepared['agent_id']==identity['agent_id'] and prepared['name']==data['caller']['name']
    assert prepared['runtime_dir']==str(owner.parent/'runtime')
    state = json.loads((owner.parent/'runtime/child-state.json').read_text())
    assert state['phase']=='prepared'
    source = str(probe.install/'bin/lib/runtime_client.py')
    entry_pids = {row['pid'] for row in rows if row['event']=='real-call'
                  and row['file']==source and row['name']=='entry_operation'}
    assert len(entry_pids)==1, 'actual shell entry was not called once'
    pid = next(iter(entry_pids))
    for name in ('_check_shell_spawn','_prepare_child_launch'):
        assert any(row['event']=='real-call' and row['file']==source
                   and row['name']==name and row['pid']==pid for row in rows), \
            'actual shell preparation was not reached in the entry process: '+name
    probe.last_shell_preparation = {'pid':pid, 'intent':intent, 'prepared':prepared}
