"""Subprocess front doors for TEST caller delivery; no replacement resolver.

Each dispatch invokes the named real entry with the literal caller inputs.
Unknown product fields must reject, not disappear inside this adapter.
"""
import json
from pathlib import Path
import sys


def setup(data):
    from runtime_operator import RuntimeOperator
    from runtime_client import RuntimeClient
    from runtime_local_configure import plan_local_configuration, apply_local_configuration
    operator = RuntimeOperator(data['server_config'])
    parent_path = operator.path_for('parent')
    if not parent_path.exists():
        operator.prepare('parent')
        RuntimeClient(parent_path).create(data['caller']['parent'], 'codex', 'fixture-parent')
    child_path = operator.prepare('child_01')
    child = RuntimeClient(child_path)
    value = plan_local_configuration(child, data['caller'], data['context'],
        python=sys.executable, cli=data['provider_binary'], declared_cli_version='fixture-pinned',
        settings_paths=[data['settings']], skill_sources=[],
        native_sources=[{'path':data['settings'], 'layer':'user'}])
    apply_local_configuration(child, value, approved_id=value['plan_id'])
    from runtime_client import runtime_delivery
    from caller_trust import prepare_codex_trust
    delivery = runtime_delivery(child)
    # Explicit fixture setup, never a native configure/discovery success.
    prepare_codex_trust(delivery, fixture_mcp=bool(data.get('fixture_mcp')))
    delivery.instruction_configure('codex')
    if data['entry'] == 'dashboard':
        from dashboard.http_operator import configure
        configure(child.isolation/'agentstack')
    return {'parent_context':str(parent_path), 'child_context':str(child_path)}


def invoke(data):
    from runtime_client import RuntimeClient
    from runtime_operator import RuntimeOperator
    caller = data['caller']
    entry = data['entry']
    parent = RuntimeClient(data['parent_context'])
    if 'real_entry_mutation' in data:
        from caller_mutations import apply_noop
        apply_noop(data)
    tools = data.get('invalid_api_tools', caller['tools'])
    # Translate spelling only at the actual API boundary. No field is omitted
    # merely because this product version has not implemented it yet.
    if entry == 'prepare-child':
        options = {**caller, 'child_client_name':'child_01', 'program':'codex',
                   'workdir':caller['cwd'], 'task_file':caller['task'],
                   'parsed_tools':tools, 'prepare_only':True,
                   'task_delivery':data['task_delivery']}
        result = parent.prepare_child(options)
        assert result['client_config'] == data['child_context'], 'caller selected another child'
        assert_real_preparation(data, 'prepared')
        return execute_prepared(data)
    if entry == 'resume-child':
        child = RuntimeClient(data['child_context'])
        child.resume_child({**caller, 'parsed_tools':tools,
                            'provider_session_id':data['provider_session_id']})
        assert_real_preparation(data, 'resume-planned')
        return execute_prepared(data, resume=True)
    if entry == 'operator':
        from copy import deepcopy
        operator = RuntimeOperator(data['server_config'])
        assert str(operator.path_for('parent')) == data['parent_context']
        requested = {**deepcopy(caller), 'tools':tools}
        if 'operator_caller_overrides' in data:
            requested.update(data['operator_caller_overrides'])
        result = operator.spawn('child_01',
            caller=requested, task_delivery=data['task_delivery'],
            parent_client_name='parent', prepare_only=data.get('operator_prepare_only',True))
        if data.get('operator_prepare_only') is False:
            # No fixture execute helper may rescue a no-op ordinary spawn.
            return result
        assert_real_preparation(data, 'prepared')
        if data.get('operator_bad_pin'):
            from runtime_client import runtime_delivery
            child = RuntimeClient(data['child_context'])
            return runtime_delivery(child).start('codex',caller['cwd'],detached=False,
                                                 expected_intent_digest='0'*64)
        if data.get('inspect_prepare_only'):
            return result
        return execute_prepared(data)
    if entry == 'dashboard':
        from caller_dashboard import invoke_http
        return invoke_http(data)
    raise AssertionError('not a Python caller front door: '+entry)


def assert_real_preparation(data, phase):
    """A successful-looking API response is not a real prepared child."""
    from runtime_client import RuntimeClient
    child = RuntimeClient(data['child_context'])
    path = child.outputs['child']
    assert child.identity and path.is_file(), 'R3_REAL_CHILD_PREPARATION_MISSING'
    state = json.loads(path.read_text())
    assert state['phase'] == phase, 'R3_REAL_CHILD_PREPARATION_PHASE_MISMATCH'
    parent = RuntimeClient(data['parent_context'])
    assert state['parent'] == {'client_name':parent.config['client_name'],
        'server_instance_id':parent.binding['expected_server_instance_id'],
        'agent_id':parent.identity['agent_id']}, 'R3_REAL_CHILD_PREPARATION_PARENT_MISMATCH'


def preregister_shell(data):
    """Real prerequisite only: task publication belongs to the later CLI."""
    from runtime_client import RuntimeClient
    caller = data['caller']
    assert caller['provider'] == 'codex', 'this delivery fixture is Codex-only'
    result = RuntimeClient(data['parent_context']).prepare_child({**caller,
        'child_client_name':'child_01', 'program':'codex', 'workdir':caller['cwd'],
        'parsed_tools':caller['tools'], 'prepare_only':True,
        'task_delivery':data['task_delivery']})
    assert result['client_config'] == data['child_context']
    assert_real_preparation(data, 'prepared')
    task = Path(data['child_context']).parent/'runtime/task.md'
    assert not task.exists() and not task.is_symlink(), 'preregister published the shell task'
    return result


def execute_prepared(data, *, resume=False, task_prepared=False, expected_intent_digest=None):
    """Deliver the admitted prepare-only result through real foreground R2.

    This TEST adapter does not implement a launcher or manufacture launch
    rows. The existing spawn-child prepare operation owns task publication;
    RuntimeDelivery.start owns window binding, phases, execute and provider-exec.
    Running that command in the foreground avoids a real tmux transport in this
    delivery-only fixture; the saved caller's detached value is not rewritten.
    """
    from runtime_client import RuntimeClient, entry_operation, runtime_delivery
    if not resume and not task_prepared:
        result = entry_operation('spawn-child', ['--context', data['parent_context'],
            '--child-client-name', 'child_01', '--prepare-only',
            '--task-file', data['caller']['task']])
        assert result == 0, 'real task preparation failed'
    child = RuntimeClient(data['child_context'])
    pinned = {} if expected_intent_digest is None else {'expected_intent_digest':expected_intent_digest}
    return runtime_delivery(child).start('codex', data['caller']['cwd'], detached=False,
        launch_kind='resume' if resume else 'startup',
        provider_session_id=data['provider_session_id'] if resume else None, **pinned)


def main():
    data = json.loads(Path(sys.argv[2]).read_text())
    if sys.argv[1] == 'setup':
        result = setup(data)
    elif sys.argv[1] == 'preregister-shell':
        assert data['entry'] == 'shell'
        result = preregister_shell(data)
    elif sys.argv[1] == 'execute-shell':
        assert data['entry'] == 'shell'
        digest = data['shell_intent_digest']
        assert isinstance(digest,str) and len(digest)==64
        result = execute_prepared(data, task_prepared=True,
            expected_intent_digest='0'*64 if data.get('shell_bad_pin') else digest)
    elif sys.argv[1] == 'execute-dashboard':
        assert data['entry'] == 'dashboard'
        digest = data['dashboard_intent_digest']
        assert isinstance(digest,str) and len(digest)==64
        result = execute_prepared(data, task_prepared=True,
            expected_intent_digest='0'*64 if data.get('dashboard_bad_pin') else digest)
    elif sys.argv[1] == 'resume-precondition':
        # Its ordinary valid spawn is separate from the tested resume inputs.
        data = {**data, 'entry':'prepare-child'}
        data.pop('invalid_api_tools', None)
        result = invoke(data)
    else:
        assert sys.argv[1] == 'invoke'
        result = invoke(data)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
