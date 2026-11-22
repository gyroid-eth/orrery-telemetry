"""Literal target/compact safety cases before touching a provider or live server."""
import json
import os
from pathlib import Path
import runpy
import pytest

ROOT=Path(__file__).resolve().parents[1]
API=runpy.run_path(str(ROOT/'bin/lib/runtime_tmux.py'))

@pytest.mark.parametrize('provider,text,state,empty,ctx,reason',[
 ('claude','✳ Stewing… (18s · ↓ 1.1k tokens)\n❯\u00a0\nfixture | Opus 5 | ctx: 33% used','work',True,33,'TMUX_TARGET_NOT_IDLE'),
 ('claude','✽ Inspecting files…\n❯\nctx: 33% used','work',True,33,'TMUX_TARGET_NOT_IDLE'),
 ('claude','✽\n Inspecting files…\n❯\nctx: 33% used','work',True,33,'TMUX_TARGET_NOT_IDLE'),
 ('claude','✻ Worked for 6s · done 6:39 PM\n❯\u00a0\nfixture | Opus 5 | ctx: 14% used','idle',True,14,None),
 ('claude','✻ Baked for 2s · done 1:07 PM\n❯ synthetic draft\nctx: 21% used','idle',False,21,'TMUX_TARGET_INPUT_NOT_EMPTY'),
 ('claude','❯ Try "fix lint errors"\nfixture | Opus 5','idle',False,None,'TMUX_TARGET_INPUT_NOT_EMPTY'),
 ('claude','Do you want to run this command?\n❯ 1. Yes\n  2. No','unknown',False,None,'TMUX_TARGET_NOT_IDLE'),
 ('claude','Enter to select · Esc to cancel\n❯ 1. Option','unknown',False,None,'TMUX_TARGET_NOT_IDLE'),
 ('codex','• Working (1s • esc to interrupt)\n›\ngpt-model medium · Context 70% left','work',True,30,'TMUX_TARGET_NOT_IDLE'),
 ('codex','›\ngpt-model medium · Context 70% left','idle',True,30,None),
 ('codex','› draft\ngpt-model medium · Context 70% used','idle',False,70,'TMUX_TARGET_INPUT_NOT_EMPTY'),
 ('codex','■ Selected model is at capacity. Please try a different model.\n›','unknown',True,None,'TMUX_TARGET_NOT_IDLE'),
 ('claude','? for shortcuts','unknown',None,None,'TMUX_TARGET_NOT_IDLE'),
 ('gemini','❯','unknown',None,None,'TMUX_TARGET_NOT_IDLE'),
])
def test_current_prompt_evidence_has_literal_provider_results(provider,text,state,empty,ctx,reason):
    reader=API['compact_readiness']
    value=reader(text,provider)
    assert value==dict(state=state,input_empty=empty,context_used_percent=ctx)
    assert API['compact_refusal'](value)==reason


def test_old_context_digest_cannot_be_reused_after_compact_and_principal_change():
    digest=API['target_digest']
    target=dict(agent_id=2,pid=101,process_start='fixture-start',socket_path='/fixture/socket')
    principal=dict(kind='parent',agent_id=1,uid=501)
    proof='a'*64
    before=dict(state='idle',input_empty=True,context_used_percent=90)
    same=digest(target,principal,proof,before)
    assert same==digest(target,dict(principal),proof,dict(before))
    assert same!=digest(target,principal,proof,{**before,'context_used_percent':10})
    assert same!=digest(target,{**principal,'kind':'operator','agent_id':None},proof,before)


def test_all_new_result_schemas_use_existing_definition_validator():
    api=runpy.run_path(str(ROOT/'packages/agentstack_mail/src/agentstack_mail/schema_contract.py'))
    for name in ('target.success','capture.success','attach.admission','compact.submitted','error.before_effect','error.outcome_unknown'):
        api['validate_schema_definition'](API['target_result_schema'](name))
    good=dict(contract='orrery-tmux-target-v1',ok=False,allowed=False,operation='send-compact',
              failure_kind='target',reason='OWNER_REQUIRED',effects='none',next_operations=[])
    api['validate_schema'](good,API['target_result_schema']('error.before_effect'))
    with pytest.raises(ValueError):
        api['validate_schema']({**good,'failure_kind':'helper'},API['target_result_schema']('error.before_effect'))


def test_shared_legacy_error_schema_has_one_authored_reason_table():
    spec=json.loads((ROOT/'bin/lib/runtime_tmux_contract.json').read_text())
    assert 'enum' not in spec['result_properties']['reason']
    assert API['result_schema']('error')['anyOf'][0]['properties']['reason']['enum']


def test_codex_only_faulty_reader_is_detected_by_claude_empty_work_literal():
    stalls=runpy.run_path(str(ROOT/'dashboard/provider_stalls.py'))
    text='✳ Stewing… (18s · ↓ 1.1k tokens)\n❯\nctx: 33% used'
    # A reader with only the old Codex work predicate cannot establish Claude idle.
    assert stalls['classify'](text)['state']!='work'
    assert API['compact_refusal'](API['compact_readiness'](text,'claude'))=='TMUX_TARGET_NOT_IDLE'


def test_real_cli_permission_negative_includes_valid_digest_before_owner_check(capsys):
    client=runpy.run_path(str(ROOT/'bin/lib/runtime_client.py'))
    code=client['main'](['send-compact','--agent-id','2','--runtime-mode','legacy','--confirm-state-digest','0'*64])
    value=json.loads(capsys.readouterr().out)
    assert code==2 and value['reason']=='OWNER_REQUIRED'
    assert value['effects']=='none'
    code=client['main'](['send-compact','--agent-id','2','--runtime-mode','legacy'])
    value=json.loads(capsys.readouterr().out)
    assert code==2 and value['reason']=='TMUX_TARGET_INPUT_INVALID'


def test_partial_install_missing_new_global_contract_keeps_legacy_caps(tmp_path,capsys):
    import shutil
    library=tmp_path/'lib';library.mkdir()
    for name in ('runtime_tmux.py','runtime_tmux_contract.json'):
        shutil.copy2(ROOT/'bin/lib'/name,library/name)
    # Definition loading of legacy consumer must not require the new target file.
    api=runpy.run_path(str(library/'runtime_tmux.py'))
    assert api['TARGET_SPEC'] is None
    api['result_schema']('legacy-target')
    with pytest.raises(api['TargetError'],match='^TMUX_HELPER_FAILED$'):
        api['target_result_schema']('target.success')


def test_generation_zero_and_nullable_readiness_are_valid_shared_schema():
    validator=runpy.run_path(str(ROOT/'packages/agentstack_mail/src/agentstack_mail/schema_contract.py'))
    value=dict(agent_id=2,agent_name='Beta',credential_generation=0,binding=dict(expected_server_instance_id='fixture',candidate_generation='fixture',authority_epoch='fixture'),pid=100,process_start='fixture-start',transport=None,socket_path=None,server_pid=None,session_id=None,session_name=None,window_id=None,pane_id=None,argv=[],environment_unset=['TMUX','TMUX_PANE'],kind='standalone-process',launch=None)
    reply=dict(contract='orrery-tmux-target-v1',ok=True,allowed=True,operation='target',principal=dict(kind='operator',uid=os.getuid(),agent_id=None),target=value,state_sha256='0'*64,next_operations=['target'],compact_readiness=dict(state='unknown',input_empty=None,context_used_percent=None))
    validator['validate_schema'](reply,API['target_result_schema']('target.success'))
    for invalid in (False,-1):
        with pytest.raises(ValueError):validator['validate_schema']({**reply,'target':{**value,'credential_generation':invalid}},API['target_result_schema']('target.success'))



def test_all_public_input_branches_use_shared_validator_and_old_type_array_is_invalid():
    api=runpy.run_path(str(ROOT/'packages/agentstack_mail/src/agentstack_mail/schema_contract.py'))
    for schema in API['TARGET_SPEC']['input_schemas'].values():
        api['validate_schema_definition'](schema)
    with pytest.raises((ValueError,TypeError)):
        api['validate_schema_definition']({'type':['boolean','null']})
