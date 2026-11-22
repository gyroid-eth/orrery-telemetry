"""Pre-implementation Tb acceptance: actual entrypoints, synthetic providers/tmux.

Intentionally red until lifecycle integration; no skip or product shim.
"""
import json
import hashlib
import runpy
import subprocess
import sys
import socket
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))
import test_global_runtime_delivery as fixtures

server = fixtures.server
api = fixtures.api


from r2_tb_v3_oracles import assert_detached, assert_retry, observe_retry, assert_retry_identity, SPEC


@pytest.fixture
def launch_probe(server, monkeypatch):
    client = fixtures.context(server, window=False)
    delivery = api['runtime_delivery'](client)
    monkeypatch.setenv('AGENTSTACK_CLAUDE_BIN', '/usr/bin/true')
    monkeypatch.delenv('TMUX', raising=False)
    calls = []
    occupied = []
    transport_dir = client.isolation / 't'
    tmux_api = api['runtime_tmux_api']()
    socket_builder = tmux_api['global_socket']
    monkeypatch.setitem(tmux_api, 'global_socket', lambda home, instance, **kw:
        socket_builder(home, instance, base=transport_dir, **{k:v for k,v in kw.items() if k!='base'}))
    expected_socket = transport_dir / (hashlib.sha256(json.dumps([client.config['agentstack_home'], client.binding['expected_server_instance_id']], separators=(',', ':')).encode()).hexdigest()+'.sock')
    assert len(__import__('os').fsencode(expected_socket)) < 104, 'fixture socket itself must be admissible'
    external = subprocess.run
    def tmux(argv, **kwargs):
        if argv[0] != 'tmux':
            return external(argv, **kwargs)
        calls.append(list(argv))
        output = '\n'.join(occupied) if 'list-sessions' in argv else ''
        return subprocess.CompletedProcess(argv, 1 if 'has-session' in argv else 0, output, '')
    monkeypatch.setattr(subprocess, 'run', tmux)
    return client, delivery, calls, occupied, transport_dir


def test_F4_01_detached_start_uses_canonical_name_and_dedicated_socket(launch_probe):
    client, delivery, calls, occupied, transport_dir = launch_probe
    delivery.start('claude', client.isolation, detached=True)
    preimage = json.dumps([client.config['agentstack_home'], client.binding['expected_server_instance_id']], separators=(',', ':'))
    expected = transport_dir / (hashlib.sha256(preimage.encode()).hexdigest()+'.sock')
    created = next(argv for argv in calls if 'new-session' in argv)
    assert_detached(created, expected, client.identity['name'])
    assert delivery.launch()['tmux_transport'] == 'global-instance'


@pytest.mark.parametrize("spelling", ["exact", "case-alias"])
def test_F4_02_foreign_name_conflict_precedes_bind(launch_probe, monkeypatch, spelling):
    client, delivery, calls, occupied, transport_dir = launch_probe
    occupied.append(client.identity['name'] if spelling == 'exact' else client.identity['name'].swapcase())
    bindings = []
    rpc = client.rpc
    def counted(method, params):
        if params.get('name') == 'bind_window_identity':bindings.append(params)
        return rpc(method, params)
    monkeypatch.setattr(client, 'rpc', counted)
    reason = SPEC['reason_delta']['canonical_name_conflict']['reason']
    with pytest.raises(RuntimeError, match='^'+reason+'$'):
        delivery.start('claude', client.isolation, detached=True)
    assert any('list-sessions' in argv and argv[:3]==['tmux','-S',str(transport_dir / (hashlib.sha256(json.dumps([client.config['agentstack_home'], client.binding['expected_server_instance_id']], separators=(',', ':')).encode()).hexdigest()+'.sock'))] for argv in calls), 'collision must consult the selected socket'
    assert bindings == [], 'foreign canonical name must be rejected before bind'
    assert not any('new-session' in argv for argv in calls)


@pytest.mark.parametrize('case', list(SPEC['retry_cases']))
def test_F4_03_retry_validates_actual_socket_target_and_wrapper(launch_probe, monkeypatch, case):
    client, delivery, calls, occupied, transport_dir = launch_probe
    row = delivery.prepare_launch('claude', cwd=client.isolation)
    expected_socket = transport_dir / (hashlib.sha256(json.dumps([client.config['agentstack_home'],client.binding['expected_server_instance_id']],separators=(',', ':')).encode()).hexdigest()+'.sock')
    state = {'proof_reads':0, 'birth_reads':0, 'provider_calls':[], 'foreign_calls':[]}
    external = subprocess.run
    peers=[]
    if case!='absent':
        transport_dir.mkdir(mode=0o700,exist_ok=True)
        peer=socket.socket(socket.AF_UNIX);peer.bind(str(expected_socket));expected_socket.chmod(0o600);peers.append(peer)
        # Proposed execution evidence is checked against its one v2 schema.
        # Current product rejects this new record: a missing-contract red,
        # never a socket-path/setup failure or ownership acceptance.
        execution={'runner':{'pid':310,'process_start':'runner-birth'},
                   'outcome':{'state':'pending','returncode':None}}
        api['wire_contract']()['validate_schema'](execution,SPEC['record_deltas']['launch_execution'])
        row.update(phase='running' if case=='owned' else 'gated',tmux_transport='global-instance',execution=execution)
        api['atomic_json'](client.outputs['launch'],row)
        live={'kind':'orrery-global-live-session-v1', **client.binding,'agent_id':client.identity['agent_id'],
              'session_id':'retry-provider','pid':400,'process_start':'provider-birth','pane_id':'%1',
              'launch_id':row['launch_id'],'receipt_nonce':row['receipt_nonce'] if case!='nonce-change' else 'different-nonce'}
        (client.runtime_dir/'live-sessions').mkdir(mode=0o700)
        api['atomic_json'](client.runtime_dir/'live-sessions/retry-provider.json',live)
    def peer_run(argv, **kwargs):
        if argv[0]=='ps':
            if '-axo' in argv:
                uid=__import__('os').getuid()
                return subprocess.CompletedProcess(argv,0,f'310 1 {uid} python\n400 310 {uid} claude\n','')
            state['birth_reads']+=1
            birth='different-birth' if case=='pid-change' else ('runner-birth' if '310' in argv else 'provider-birth')
            return subprocess.CompletedProcess(argv,0,birth+'\n','')
        if argv[0]!='tmux':return external(argv,**kwargs)
        calls.append(list(argv))
        if any(action in argv for action in ('kill-session','kill-pane','send-keys','paste-buffer')):
            state['foreign_calls'].append(list(argv))
        name=client.identity['name']
        if 'has-session' in argv:return subprocess.CompletedProcess(argv,1 if case=='absent' else 0,'','')
        if 'list-sessions' in argv:return subprocess.CompletedProcess(argv,0,'' if case=='absent' else name+'\n','')
        if 'display-message' in argv or 'list-panes' in argv:
            state['proof_reads']+=1
            pane_pid=999 if case=='foreign' else 310
            fmt=argv[-1]
            out=('300' if fmt=='#{pid}' else '$1\t'+name+'\t%1\t'+str(pane_pid))
            return subprocess.CompletedProcess(argv,0,out+'\n','')
        return subprocess.CompletedProcess(argv,0,'','')
    monkeypatch.setattr(subprocess,'run',peer_run)
    def no_provider(*args, **kwargs):
        state['provider_calls'].append(args)
        raise AssertionError('retry must not execute a second provider')
    monkeypatch.setattr(subprocess,'Popen',no_provider)
    # Scope the OS spy to this component, never replace shared os.kill.
    original_os=delivery.execute.__globals__['os']
    def no_signal(*args, **kwargs):state['foreign_calls'].append(args)
    scoped_os=types.SimpleNamespace(**{**vars(original_os),'kill':no_signal,'killpg':no_signal,'execvpe':no_provider})
    monkeypatch.setitem(delivery.execute.__globals__,'os',scoped_os)
    try:
        status=delivery.status(row['launch_id'])
        observed=observe_retry(delivery,{'launch_id':row['launch_id'],
            'expected_state_sha256':status['state_sha256']})
        assert_retry({**observed,
            'commands':calls,'created':sum('new-session' in a for a in calls),
            'gate_submissions':sum('wait-for' in a for a in calls),'provider_execs':len(state['provider_calls']),'foreign_effects':len(state['foreign_calls']),
            'wrapper_proofs':int(state['proof_reads']>0 and state['birth_reads']>=2)},
            case,expected_socket,client.identity['name'],row['launch_id'],row['receipt_nonce'])
    finally:
        for peer in peers:peer.close()


@pytest.fixture
def ending_child(server, monkeypatch):
    parent = fixtures.context(server, window=False)
    # The isolated receiver explicitly accepts reports; CONTACT_BLOCKED must
    # not masquerade as the lost-success-response lifecycle under test.
    parent.call('set_contact_policy', {'policy':'open'})
    prepared = parent.prepare_child({'child_client_name':'child_01', 'name':'ChildAlpha',
                                    'prepare_only':True, 'program':'claude-code', 'model':'fixture'})
    child = api['RuntimeClient'](prepared['client_config'])
    monkeypatch.setenv('AGENTSTACK_CLAUDE_BIN', '/usr/bin/true')
    monkeypatch.delenv('TMUX', raising=False)
    monkeypatch.delenv('TMUX_PANE', raising=False)
    monkeypatch.setitem(api['RuntimeClient'].record_live.__globals__, 'process_start', lambda *a, **kw:'fixture-birth')
    delivery = api['runtime_delivery'](child)
    row = delivery.prepare_launch('claude', cwd=child.isolation)
    delivery.bind_window()
    row['phase'] = 'exec-unknown'
    delivery.save(child.outputs['launch'], row, 'launch_intent')
    transcript = child.isolation / 'terminal-transcript.jsonl'
    transcript.write_text(json.dumps({'type':'user','message':{'role':'user','content':'Delegated child agent'}})+'\n'+
        json.dumps({'type':'assistant','message':{'role':'assistant','content':[{'type':'text','text':'Unable to proceed.'}], 'stop_reason':'end_turn'}})+'\n')
    payload = {'session_id':'tb-ended', 'provider_pid':123, 'cwd':str(child.isolation), 'transcript_path':str(transcript)}
    delivery.session_start(payload, trusted_receipt=(row['launch_id'], row['receipt_nonce']))
    return parent, child, delivery, payload


@pytest.mark.parametrize('loss', ['before-http', 'after-commit'])
def test_F5_01_unknown_report_never_retires_child(ending_child, monkeypatch, loss):
    parent, child, delivery, payload = ending_child
    rpc = child.rpc
    def lose(method, params):
        if params.get('name') == 'send_message':
            if loss == 'after-commit':
                committed=api['result_value'](rpc(method, params))
                assert committed['message_id']>0
            raise api['ClientError']('TRANSPORT_FAILED')
        return rpc(method, params)
    monkeypatch.setattr(child, 'rpc', lose)
    try:child.end_session(payload)
    except api['ClientError']:pass
    current = child.management('inspect', agent_id=child.identity['agent_id'])
    assert current['retired_at'] is None, 'unconfirmed terminal report must preserve active owner'
    assert child.outputs['mutation'].exists(), 'report bytes must survive for same-intent replay'
    assert not api['read_json'](child.runtime_dir/'live-sessions/tb-ended.json')['ended']


def test_F5_02_double_end_has_one_parent_report(ending_child):
    parent, child, delivery, payload = ending_child
    child.end_session(payload)
    child.end_session(payload)
    rows = parent.call('fetch_inbox', {'include_bodies':True, 'limit':100})
    terminal = [row for row in rows if row.get('sender', {}).get('agent_id') == child.identity['agent_id']]
    assert len(terminal) == 1, 'hook and runner must share one terminal report intent'


def test_F5_03_end_cli_is_discoverable_without_side_effects():
    with pytest.raises(SystemExit) as caught:
        api['main'](['end', '--help'])
    assert caught.value.code == 0


def test_instrument_rejects_wrong_socket_name_and_attach_adoption():
    socket = Path('/synthetic/instance.sock')
    correct = ['tmux','-S',str(socket),'new-session','-d','-s','Alpha']
    assert_detached(correct,socket,'Alpha')
    mutants = [['tmux',*correct[3:]], correct[:-1]+['orrery-old-uuid'],correct+['-A'], [x for x in correct if x!='-d']]
    for mutant in mutants:
        with pytest.raises(AssertionError):assert_detached(mutant,socket,'Alpha')


@pytest.mark.parametrize('mutant',['saved-uuid','saved-nonce','returned-uuid','returned-phase'])
def test_F4_03_caller_observes_saved_and_returned_mutations(launch_probe,monkeypatch,mutant):
    client,delivery,*_=launch_probe
    row=delivery.prepare_launch('claude',cwd=client.isolation)
    original=dict(row)
    def changed(operation,request):
        saved=dict(original)
        reply={'launch_id':original['launch_id'],'phase':original['phase']}
        if mutant=='saved-uuid':saved['launch_id']='11111111-2222-4333-8444-555555555555'
        if mutant=='saved-nonce':saved['receipt_nonce']='e'*64
        if mutant=='returned-uuid':reply['launch_id']='11111111-2222-4333-8444-555555555555'
        if mutant=='returned-phase':reply['phase']='ended'
        api['atomic_json'](client.outputs['launch'],saved)
        return reply
    monkeypatch.setattr(delivery,'operator',changed)
    observed=observe_retry(delivery,{})
    with pytest.raises(AssertionError):assert_retry_identity(observed,original['launch_id'],original['receipt_nonce'])
