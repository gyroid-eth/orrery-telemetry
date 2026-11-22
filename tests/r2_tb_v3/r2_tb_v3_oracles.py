"""Acceptance oracles only; never a runtime resolver or policy implementation."""
import json
import os
from pathlib import Path

SPEC=json.loads((Path(__file__).resolve().parents[2]/'tests/fixtures/r2-tb-lifecycle-v3.json').read_text())


def assert_detached(argv, socket, name):
    assert argv[:4] == ['tmux','-S',str(socket),'new-session']
    assert '-d' in argv and '-A' not in argv
    assert argv[argv.index('-s')+1] == name


def assert_retry(trace, case, socket, name, launch_id, nonce):
    expected=SPEC['retry_cases'][case]
    assert_retry_identity(trace,launch_id,nonce)
    assert trace['reason']==expected['reason']
    assert trace['phase']==expected['phase']
    assert trace['created']==expected['created']
    assert trace['gate_submissions']==expected['gate_submissions']
    assert trace['provider_execs']==0
    assert trace['foreign_effects']==0
    commands=trace['commands']
    assert commands, 'status-only/no-op retry is not target admission'
    for argv in commands:
        assert argv[:3]==['tmux','-S',str(socket)]
        if '-t' in argv:
            assert argv[argv.index('-t')+1] in ('='+name,'$1','%1'), 'unresolved or wrong target'
        if 'new-session' in argv:
            import shlex
            assert_detached(argv,socket,name)
            wrapper=shlex.split(argv[-1])
            index=wrapper.index('execute-launch')
            assert wrapper[index+1:index+3]==[launch_id,nonce]
    if case=='owned':
        assert trace['wrapper_proofs']==1, 'name alone is not launch/nonce/PID evidence'


def assert_role_projection(actual, case):
    assert actual['purpose']==case['purpose']
    assert actual['signal_pid']==case['signal_pid']
    assert actual['runner_signals']==0
    assert actual['cleanup_while_provider_live']==0


def observe_retry(delivery, request):
    """Observe the real saved record AFTER the caller, including refusal paths."""
    path=delivery.client.outputs['launch']
    before=path.read_bytes()
    result=None;reason=None
    try:result=delivery.operator('launch-retry',request)
    except RuntimeError as exc:reason=str(exc)
    after=delivery.launch()
    return {'launch_id':after['launch_id'],'nonce':after['receipt_nonce'],'phase':after['phase'],
            'reason':reason,'returned':result,'before_bytes':before,'after_bytes':path.read_bytes()}


def assert_retry_identity(trace, launch_id, nonce):
    assert trace['launch_id']==launch_id and trace['nonce']==nonce
    if trace['reason'] is not None:
        assert trace['before_bytes']==trace['after_bytes'], 'refusal changed saved launch'
    else:
        result=trace['returned']
        assert result['launch_id']==launch_id
        assert result['phase']==trace['phase']
        if 'receipt_nonce' in result:assert result['receipt_nonce']==nonce


def target_schema(name='target.lifecycle.success'):
    """Materialize a proposal schema; never installed into product/CLI globals."""
    import copy
    import runpy
    import types
    root=Path(__file__).resolve().parents[2]
    api=runpy.run_path(str(root/'bin/lib/runtime_tmux.py'))
    contract=copy.deepcopy(api['TARGET_SPEC'])
    if name=='target.lifecycle.success':
        delta=SPEC['target_contract_delta'];base=contract['schema_atoms']['target']['anyOf']
        kinds={row['properties']['kind']['const']:row for row in base}
        variants=[]
        for spec in delta['target_variants']:
            row=copy.deepcopy(kinds[spec['inherit_kind']])
            for key,value in spec['properties'].items():
                if 'inherit_kind' in value:value=kinds[value['inherit_kind']]['properties'][value['property']]
                row['properties'][key]=copy.deepcopy(value)
            variants.append(row)
        atoms=contract['schema_atoms']
        atoms.update(copy.deepcopy(delta['schema_atom_additions']))
        atoms['lifecycle_target']={'anyOf':variants}
        spec=delta['result_additions'][name]
        row=copy.deepcopy(contract['result_tables'][spec['inherit_result']])
        row['fields']+=spec['append_fields']
        for field,atom in spec['atom_overrides'].items():atoms[field]=atoms[atom]
        contract['result_tables'][name]=row
    fn=api['target_result_schema']
    # Reuse canonical derivation code against a test-owned contract value.
    fn=types.FunctionType(fn.__code__,{**fn.__globals__,'TARGET_SPEC':contract})
    return fn(name)
