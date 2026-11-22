"""Actual FirstTurn engine with verified-parent fixture input, not a copy."""
import copy
import json
from pathlib import Path
import runpy

import pytest

ROOT=Path(__file__).resolve().parents[2]
SPEC=json.loads((ROOT/'tests/fixtures/r2-tb-lifecycle-v3.json').read_text())
CLASSIFIER=runpy.run_path(str(ROOT/'hooks/claude-initial-task-status.py'))


def receipt(recipients):
    return {'server_instance_id':'fixture-instance','candidate_generation':'fixture-candidate',
        'authority_epoch':'fixture-epoch','agent_id':11,'credential_generation':1,
        'request_id':'11111111-2222-4333-8444-555555555555','mutation_revision':2,
        'result_as_of':'commit','committed_at':'2026-10-15T00:00:00+00:00','message_id':21,
        'reply_to':None,'message_sha256':'a'*64,'recipient_agent_ids':recipients,
        'durability':'db-committed','outputs_as_of_commit':'pending'}


@pytest.mark.parametrize('vector',SPEC['classifier_vectors'],ids=lambda v:v['id'])
def test_F5_10_global_parent_report_uses_existing_classifier(vector):
    turn=CLASSIFIER['FirstTurn']('fixture task','ParentFixture')
    # A classifier unit input. Caller/child-state validation is a separate gate;
    # this does not fake a successful classifier or normalize recipients itself.
    if vector['context']=='global':turn.parent_agent_id=7
    turn.feed({'type':'user','message':{'content':'fixture task'}})
    arguments={'to':vector['legacy_to']} if 'legacy_to' in vector else {'to_agent_ids':vector['recipients']}
    turn.feed({'type':'assistant','message':{'id':'tool-message','stop_reason':'tool_use','content':[
        {'type':'tool_use','id':'call-report','name':'mcp__orrery-mail__send_message','input':arguments}]}})
    if vector['result']!='unknown':
        payload=receipt(vector.get('receipt_recipients',vector.get('recipients',[7])))
        turn.feed({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'call-report',
            'is_error':vector['result']=='error','content':json.dumps(payload)}]}})
    turn.feed({'type':'assistant','message':{'id':'finished','stop_reason':'end_turn',
                                           'content':[{'type':'text','text':'Finished.'}]}})
    assert turn.status()==vector['expected']


def test_F5_10_fixture_success_has_actual_s2b_response_schema():
    schema=json.loads((ROOT/'packages/agentstack_mail/fixtures/global-server-s2b.json').read_text())['tool_contracts']['send_message']['output_schema']
    validate=runpy.run_path(str(ROOT/'packages/agentstack_mail/src/agentstack_mail/schema_contract.py'))['validate_schema']
    validate(receipt([7]),schema)
