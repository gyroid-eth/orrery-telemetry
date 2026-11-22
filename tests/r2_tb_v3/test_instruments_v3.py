"""Independent acceptance instruments, not a lifecycle implementation."""
import copy
import json
from pathlib import Path
import runpy

import pytest

ROOT=Path(__file__).resolve().parents[2]
SPEC=json.loads((ROOT/'tests/fixtures/r2-tb-lifecycle-v3.json').read_text())
validator=runpy.run_path(str(ROOT/'packages/agentstack_mail/src/agentstack_mail/schema_contract.py'))


def test_all_proposed_schemas_use_existing_shared_validator():
    for group in ('input_schemas','output_delta','record_deltas'):
        for schema in SPEC[group].values():
            if isinstance(schema,dict):validator['validate_schema_definition'](schema)
    for entry,schema in SPEC['input_schemas'].items():
        request={'entry':entry,'agent_id':7,'confirm_state_digest':'a'*64}
        if entry=='replay-end':request['expected_intent_sha256']='b'*64
        validator['validate_schema'](request,schema)
        if entry=='replay-end':validator['validate_schema']({**request,'expected_intent_sha256':None},schema)
        for field in schema['required']:
            broken=copy.deepcopy(request);del broken[field]
            with pytest.raises(ValueError, match="ARGUMENTS_UNSUPPORTED"):validator['validate_schema'](broken,schema)
        for key,value in [('agent_id',True),('agent_id',0),('confirm_state_digest','wrong'),('name','Alpha'),('shell','rm')]:
            broken={**request,key:value}
            with pytest.raises(ValueError, match="ARGUMENTS_UNSUPPORTED"):validator['validate_schema'](broken,schema)


def assert_termination(trace):
    report=trace['report']
    if report=='pending':
        assert trace['active'] and not trace['ended']
        assert trace['pending_bytes']==b'original report bytes'
        assert trace['replay_uuid']==trace['uuid']
    else:
        assert report in ('confirmed','not-required','definite-rejected')
        assert trace['phase_saved_before_pending_removed']
    assert trace['reports']<=1
    assert trace['foreign_signals']==0
    assert trace['other_session_changed'] is False


def test_F5_oracle_detects_early_retire_rewritten_intent_and_foreign_signal():
    correct={'report':'pending','active':True,'ended':False,'pending_bytes':b'original report bytes',
        'uuid':'fixed-request','replay_uuid':'fixed-request','reports':1,'foreign_signals':0,'other_session_changed':False}
    assert_termination(correct)
    mutations=[{'active':False},{'ended':True},{'pending_bytes':b'regenerated'},
               {'replay_uuid':'new-request'},{'reports':2},{'foreign_signals':1},{'other_session_changed':True}]
    for change in mutations:
        with pytest.raises(AssertionError):assert_termination({**correct,**change})
    confirmed={**correct,'report':'confirmed','phase_saved_before_pending_removed':True}
    assert_termination(confirmed)
    with pytest.raises(AssertionError):assert_termination({**confirmed,'phase_saved_before_pending_removed':False})


def test_F4_03_oracle_rejects_each_wrong_caller_even_with_same_uuid():
    from r2_tb_v3_oracles import assert_retry
    socket=Path('/synthetic/fixed.sock')
    for case,expected in SPEC['retry_cases'].items():
        command=['tmux','-S',str(socket),'new-session','-d','-s','Alpha','python execute-launch launch nonce'] if case=='absent' else ['tmux','-S',str(socket),'display-message','-t','=Alpha','#{pane_pid}']
        trace={'launch_id':'launch','nonce':'nonce','phase':expected['phase'],'reason':expected['reason'],
            'commands':[command],'created':expected['created'],'gate_submissions':0,'provider_execs':0,
            'foreign_effects':0,'wrapper_proofs':1,'before_bytes':b'original','after_bytes':b'original',
            'returned':{'launch_id':'launch','phase':expected['phase']}}
        assert_retry(trace,case,socket,'Alpha','launch','nonce')
        wrong_socket=copy.deepcopy(trace);wrong_socket['commands'][0][2]='/wrong/socket'
        wrong_target=copy.deepcopy(trace);wrong_target['commands'][0][-2]='Foreign'
        mutants=[wrong_socket,wrong_target,{**trace,'commands':[]},{**trace,'provider_execs':1},
                 {**trace,'gate_submissions':1},{**trace,'foreign_effects':1},{**trace,'nonce':'changed'}]
        if case=='owned':mutants.append({**trace,'wrapper_proofs':0})
        if expected['reason'] is not None:
            mutants.extend([{**trace,'reason':None},{**trace,'after_bytes':b'changed saved record'}])
        if case=='absent':
            wrong_wrapper=copy.deepcopy(trace);wrong_wrapper['commands'][0][-1]='python execute-launch different nonce'
            mutants.append(wrong_wrapper)
        for mutant in mutants:
            with pytest.raises(AssertionError):assert_retry(mutant,case,socket,'Alpha','launch','nonce')


def test_F4_02_length_failure_cannot_masquerade_as_name_collision(tmp_path):
    api=runpy.run_path(str(ROOT/'bin/lib/runtime_tmux.py'))
    long_base=tmp_path/('x'*80)
    with pytest.raises(RuntimeError,match='^TMUX_SOCKET_PATH_TOO_LONG$'):
        api['global_socket'](tmp_path.resolve(),'fixture-instance',base=long_base)
    assert SPEC['reason_delta']['canonical_name_conflict']['reason']!='TMUX_SOCKET_PATH_TOO_LONG'


def test_F4_07_role_oracle_rejects_runner_signals_and_false_provider_liveness():
    from r2_tb_v3_oracles import assert_role_projection
    for case in SPEC['role_cases']:
        correct={'purpose':case['purpose'],'signal_pid':case['signal_pid'],'runner_signals':0,'cleanup_while_provider_live':0}
        assert_role_projection(correct,case)
        for change in [{'runner_signals':1},{'signal_pid':310},{'cleanup_while_provider_live':1}]:
            with pytest.raises(AssertionError):assert_role_projection({**correct,**change},case)
        if case['purpose']!='runtime':
            with pytest.raises(AssertionError):assert_role_projection({**correct,'purpose':'runtime'},case)


def test_complete_lifecycle_target_schema_and_runtime_shape():
    from r2_tb_v3_oracles import target_schema
    life=target_schema()
    runtime=target_schema('target.success')
    validator['validate_schema_definition'](life)
    validator['validate_schema_definition'](runtime)
    base={'agent_id':7,'agent_name':'Alpha','credential_generation':1,
          'binding':{'expected_server_instance_id':'instance','candidate_generation':'candidate','authority_epoch':'epoch'},
          **{k:None for k in ('pid','process_start','transport','socket_path','server_pid','session_id','session_name','window_id','pane_id')},
          'argv':[],'environment_unset':['TMUX','TMUX_PANE']}
    for kind,launch in [('launch-lifecycle',{'launch_id':'saved-launch','receipt_nonce':'saved-nonce'}),('standalone-lifecycle',None)]:
        value={'contract':'orrery-tmux-target-v1','ok':True,'allowed':True,'operation':'target',
          'principal':{'kind':'parent','uid':501,'agent_id':2},'target':{**base,'kind':kind,'launch':launch},
          'state_sha256':'a'*64,'next_operations':['target','replay-end'],
          'compact_readiness':{'state':'unknown','input_empty':None,'context_used_percent':None},
          'lifecycle':{'phase':'report-pending','provider_outcome':'exited','pending_sha256':'b'*64}}
        validator['validate_schema'](value,life)
        for field in value:
            broken=copy.deepcopy(value);del broken[field]
            with pytest.raises(ValueError):validator['validate_schema'](broken,life)
        for mutation in [{'pid':1},{'process_start':'fake-birth'},{'pane_id':'%1'}, {'kind':'launched-tmux'}, {'launch':None if launch else {'launch_id':'fake','receipt_nonce':'fake'}}]:
            with pytest.raises(ValueError):validator['validate_schema']({**value,'target':{**value['target'],**mutation}},life)
        for op in ('capture','attach','send-compact','end'):
            with pytest.raises(ValueError):validator['validate_schema']({**value,'next_operations':['target',op]},life)
        # The unmodified runtime branch cannot silently swallow lifecycle fields.
        with pytest.raises(ValueError):validator['validate_schema'](value,runtime)
    ordinary={**base,'kind':'standalone-process','pid':123,'process_start':'birth','launch':None}
    value.pop('lifecycle');value['target']=ordinary;value['next_operations']=['target']
    validator['validate_schema'](value,runtime)
