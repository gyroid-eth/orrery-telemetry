"""Local launch projection is readonly and is never native admission proof."""
from pathlib import Path
from types import SimpleNamespace
import os
import uuid

import pytest

from test_runtime_local_configure import prepared, configure, plan, server
from test_runtime_configure_ownership import snapshot
import runtime_local_launch as launch


def configured(prepared,monkeypatch):
    client,workspace,inputs = prepared
    value = plan(prepared)
    configure.apply_local_configuration(client,value,approved_id=value['plan_id'])
    monkeypatch.setenv('AGENTSTACK_CODEX_BIN',inputs['cli'])
    return client,workspace,inputs,value['intent']


def test_saved_projection_is_readonly_but_native_admission_stays_closed(prepared,monkeypatch):
    client,workspace,inputs,intent = configured(prepared,monkeypatch)
    before = snapshot(Path(client.isolation))
    checked = launch.inspect_local_launch(client,'codex',workspace.caller['cwd'])
    assert checked['intent'] == intent
    assert checked['projection']['execution']['argv'][0] == '--model=fixture-glm-model'
    assert checked['binary'] == inputs['cli']
    assert checked['projection']['requires_native_validation'] is True
    with pytest.raises(launch.LocalModelError,match='ADAPTER_UNSUPPORTED'):
        launch.admit_local_launch(client,'codex',workspace.caller['cwd'])
    assert snapshot(Path(client.isolation)) == before
    assert not list(Path(client.isolation).rglob('must-not-run'))


@pytest.mark.parametrize('mutation,reason',[
    ('receipt','INTENT_CHANGED'),('provider','IDENTITY_MISMATCH'),('cwd','IDENTITY_MISMATCH'),
    ('cli-selection','CLI_CHANGED'),('cli-bytes','AUTH_FAILED'),
    ('task','TASK_CHANGED'),('profile','PROFILE_CHANGED')])
def test_saved_projection_rejects_drift_without_writes(prepared,monkeypatch,mutation,reason):
    client,workspace,inputs,intent = configured(prepared,monkeypatch)
    options = {'expected_digest':intent['intent_digest']}
    provider,cwd = 'codex',workspace.caller['cwd']
    if mutation == 'receipt':options['expected_digest'] = '0'*64
    elif mutation == 'provider':provider = 'claude'
    elif mutation == 'cwd':cwd = str(workspace.tmp)
    elif mutation == 'cli-selection':monkeypatch.setenv('AGENTSTACK_CODEX_BIN','/usr/bin/false')
    elif mutation == 'cli-bytes':
        binary = Path(inputs['cli']);binary.chmod(0o600);binary.write_text('changed');binary.chmod(0o500)
    elif mutation == 'task':workspace.task.write_text('changed task')
    else:workspace.profile.write_text(workspace.profile.read_text().replace('131072','262144'))
    before = snapshot(Path(client.isolation))
    with pytest.raises(launch.LocalModelError,match=reason):
        launch.inspect_local_launch(client,provider,cwd,**options)
    assert snapshot(Path(client.isolation)) == before


def test_receipt_task_hash_and_missing_digest_cannot_adopt_saved_intent(prepared,monkeypatch):
    client,workspace,inputs,intent=configured(prepared,monkeypatch)
    before=snapshot(Path(client.isolation))
    for digest,task_hash,reason in ((None,intent['task_sha256'],'INTENT_CHANGED'),
                                  (intent['intent_digest'],'0'*64,'TASK_CHANGED')):
        with pytest.raises(launch.LocalModelError,match=reason):
            launch.inspect_local_launch(client,'codex',workspace.caller['cwd'],require_receipt=True,
                expected_digest=digest,expected_task_sha256=task_hash)
    assert snapshot(Path(client.isolation))==before


@pytest.mark.parametrize('relative',[
    'bin/lib/runtime_delivery.py','bin/lib/instruction_blocks.py','hooks/child_tools.py',
    'bin/lib/process_observation.py'])
def test_fixed_installed_renderer_is_pinned_even_when_producer_is_loaded_separately(prepared,monkeypatch,relative):
    original,workspace,inputs=prepared
    renderer=Path(original.config['wrapper_root'])/relative
    if not renderer.exists():
        # Complete this fixture's real fixed payload before explicit approval;
        # no alternate layout/symlink or production source gate is substituted.
        source=Path(__file__).resolve().parents[1]/relative
        renderer.write_bytes(source.read_bytes())
    client,workspace,inputs,intent=configured(prepared,monkeypatch)
    assert str(renderer) in intent['configuration_evidence']['renderer_sources']
    renderer.write_text(renderer.read_text()+'\n# changed after configure\n')
    before=snapshot(Path(client.isolation))
    with pytest.raises(launch.LocalModelError,match='AUTH_FAILED'):
        launch.inspect_local_launch(client,'codex',workspace.caller['cwd'])
    assert snapshot(Path(client.isolation))==before


def test_absent_local_owner_cannot_fall_back_to_cloud(tmp_path):
    client = SimpleNamespace(mode='global',profile_path=tmp_path/'profile.json',identity={'model':'local:fixture'})
    with pytest.raises(launch.LocalModelError,match='INTENT_OWNER_REQUIRED'):
        launch.inspect_local_launch(client,'codex',str(tmp_path))
    client.identity = {'model':'gpt-example'}
    assert launch.inspect_local_launch(client,'codex',str(tmp_path)) is None
    with pytest.raises(launch.LocalModelError,match='INTENT_OWNER_REQUIRED'):
        launch.inspect_local_launch(client,'codex',str(tmp_path),expected_digest='0'*64)


def test_minimal_environment_has_no_ambient_secret_or_per_launch_pid(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY','ambient-secret')
    monkeypatch.setenv('OPENAI_API_KEY','ambient-secret')
    monkeypatch.setenv('AGENTSTACK_PROVIDER_PID','999')
    checked = {'home':'/fixed/provider','intent':{'provider':'claude'},'binary':'/approved/claude',
               'projection':{'execution':{'env':{'ANTHROPIC_MODEL':'local-model'}}}}
    runtime = {key:'fixed-'+key for key in (
        'AGENTSTACK_CLIENT_CONFIG','AGENTSTACK_RUNTIME_MODE','AGENTSTACK_LAUNCH_ID',
        'AGENTSTACK_LAUNCH_NONCE','AGENTSTACK_RUNTIME_DIR','AGENTSTACK_MCP_URL',
        'AGENT_NAME','AGENTSTACK_HOME','AGENTSTACK_AGENT_ID')}
    base = {'HOME':'/fixed/provider','CODEX_HOME':'/fixed/provider','CLAUDE_CONFIG_DIR':'/fixed/provider',
            'PATH':'/usr/bin:/bin','LANG':'C','ANTHROPIC_MODEL':'local-model',
            'AGENTSTACK_CLAUDE_BIN':'/approved/claude',**runtime}
    assert launch.local_environment(checked,runtime) == base
    assert launch.local_environment(checked,runtime,provider_pid=123) == {**base,'AGENTSTACK_PROVIDER_PID':'123'}
    for bad in ({**runtime,'OPENAI_API_KEY':'injected'}, {k:v for k,v in runtime.items() if k!='AGENT_NAME'}):
        with pytest.raises(launch.LocalModelError):launch.local_environment(checked,bad)


def test_local_envelope_adds_projection_without_changing_resume_prefix():
    checked={'intent':{'provider':'codex','cwd':'/approved/cwd'},'binary':'/approved/codex',
             'projection':{'execution':{'argv':['--model=local-model','-c','model_provider="local_fixture"']}}}
    row={'provider':'codex','cwd':'/approved/cwd','launch_kind':'startup'}
    proxy=['/approved/python','/approved/runtime_client.py','--context','/approved/context.json','proxy']
    fixed=['/approved/codex','-C','/approved/cwd','-c','check_for_update_on_startup=false',
        '-c','mcp_servers.orrery-mail.command="/approved/python"',
        '-c','mcp_servers.orrery-mail.args=["/approved/runtime_client.py", "--context", "/approved/context.json", "proxy"]',
        '-c','plugins."fixture-plugin".mcp_servers.agentstack.enabled=false']
    options={'proxy':proxy,'plugin_id':'fixture-plugin','tool_flags':['-c','fixture_tool=true'],
             'prompt':'task','tool_prompt':'tools'}
    tail=['--model=local-model','-c','model_provider="local_fixture"','-c','fixture_tool=true']
    assert launch.local_provider_command(checked,row,**options) == fixed+tail+['task\n\ntools']
    row['launch_kind']='resume'
    assert launch.local_provider_command(checked,row,provider_session_id='saved-session',**options) == fixed+['resume','saved-session']+tail
    assert launch.local_provider_command(checked,row,provider_session_id='saved-session',headless=True,**options) == fixed+['exec','resume','saved-session']+tail


def test_registration_admission_runs_before_pending_or_network(prepared,monkeypatch):
    client,workspace,inputs,intent = configured(prepared,monkeypatch)
    before=snapshot(Path(client.isolation))
    with pytest.raises(client._fixture_api['ClientError'],match='ADAPTER_UNSUPPORTED'):
        client.register_intent(workspace.caller['name'],'codex','local:fixture')
    assert snapshot(Path(client.isolation)) == before
    assert not client.outputs['registration'].exists()


@pytest.mark.parametrize('program,provider',[
    ('codex','codex'),('codex-cli','codex'),('claude','claude'),('claude-code','claude')])
def test_registration_provider_aliases_reach_the_canonical_native_gate(prepared,monkeypatch,program,provider):
    import hashlib
    client,workspace,inputs=prepared
    workspace.caller['provider']=provider
    if provider=='claude':workspace.settings.write_text('{}')
    workspace.context['approved_settings_sha256']=hashlib.sha256(workspace.settings.read_bytes()).hexdigest()
    client,workspace,inputs,intent=configured((client,workspace,inputs),monkeypatch)
    monkeypatch.setenv('AGENTSTACK_'+provider.upper()+'_BIN',inputs['cli'])
    class ReachedNativeGate(Exception):pass
    observed=[]
    def gate(proposal):
        observed.append(proposal)
        raise ReachedNativeGate()
    monkeypatch.setattr(launch.runtime_native_scope,'require_native_binding',gate)
    before=snapshot(Path(client.isolation))
    with pytest.raises(ReachedNativeGate):
        launch.admit_local_registration(client,workspace.caller['name'],program,'local:fixture')
    assert observed==[intent['effective_scope']['native_proposal']]
    assert snapshot(Path(client.isolation))==before


def test_exports_maps_malformed_saved_model_to_fixed_client_error_without_network(prepared,monkeypatch):
    from local_model_auth import intent_digest
    client,workspace,inputs,intent=configured(prepared,monkeypatch)
    frame=client._fixture_api['read_json'](client.profile_path)
    del frame['launch_intent']['model']['ref']
    frame['launch_intent']['intent_digest']=intent_digest(frame['launch_intent'])
    client._fixture_api['atomic_json'](client.profile_path,frame)
    monkeypatch.setattr(client,'reconnect',lambda **kw:pytest.fail('malformed owner reached reconnect'))
    before=snapshot(Path(client.isolation))
    with pytest.raises(client._fixture_api['ClientError'],match='LOCAL_MODEL_INTENT_CHANGED'):
        client.exports('codex')
    assert snapshot(Path(client.isolation))==before


@pytest.mark.parametrize('explicit_model',[False,True])
def test_reconnect_rechecks_saved_selection_before_refresh_or_registration(prepared,monkeypatch,explicit_model):
    original,workspace,inputs=prepared
    client=original._fixture_api['RuntimeClient'](original.path)
    client._fixture_api=original._fixture_api
    client.create(workspace.caller['name'],'codex','fixture-glm-model')
    client,workspace,inputs,intent=configured((client,workspace,inputs),monkeypatch)
    def forbidden(*args,**kwargs):pytest.fail('reconnect network before native admission')
    monkeypatch.setattr(client,'call',forbidden)
    monkeypatch.setattr(client,'capabilities',forbidden)
    before=snapshot(Path(client.isolation))
    options={'program':'codex','model':'local:fixture'} if explicit_model else {}
    with pytest.raises(client._fixture_api['ClientError'],match='ADAPTER_UNSUPPORTED'):
        client.reconnect(**options)
    assert snapshot(Path(client.isolation))==before


def test_finalize_rechecks_retained_intent_before_owner_or_network(prepared,monkeypatch):
    client,workspace,inputs,intent=configured(prepared,monkeypatch)
    pending={'kind':client._fixture_api['REG_PENDING_KIND'],**client.binding,
             'name':workspace.caller['name'],'program':'codex','model':'fixture-glm-model',
             'task_description':'fixture','registration_token':'fixture-token'}
    client.write_output(client.outputs['registration'],pending,'registration')
    def forbidden(*args,**kwargs):pytest.fail('finalize network before native admission')
    monkeypatch.setattr(client,'management',forbidden)
    monkeypatch.setattr(client,'rpc',forbidden)
    before=snapshot(Path(client.isolation))
    with pytest.raises(client._fixture_api['ClientError'],match='ADAPTER_UNSUPPORTED'):
        client.finalize_registration(42)
    assert snapshot(Path(client.isolation))==before


@pytest.fixture
def operator_server(monkeypatch):
    from test_global_child_client import server as fixture_server
    yield from fixture_server.__wrapped__(monkeypatch)


@pytest.mark.parametrize('model,reason',[
    ('local:fixture','ADAPTER_UNSUPPORTED'),('gpt-6-luna','INTENT_CHANGED')])
def test_operator_admission_does_not_create_an_operation_lock(operator_server,monkeypatch,model,reason):
    from test_runtime_local_configure import prepared as fixture_prepared
    from runtime_operator import RuntimeOperator
    client,workspace,inputs,intent=configured(
        fixture_prepared.__wrapped__(operator_server,monkeypatch),monkeypatch)
    operator=RuntimeOperator(operator_server['config_path'],api=client._fixture_api)
    lock=client.outputs['operation_lock']
    assert lock.exists()
    lock.unlink()  # Explicit fixture preparation ended; no operation holds it.
    def forbidden(*args,**kwargs):pytest.fail('operator lookup before local admission')
    monkeypatch.setattr(operator,'lookup',forbidden)
    before=snapshot(Path(client.isolation))
    with pytest.raises(client._fixture_api['ClientError'],match=reason):
        operator.create(client.config['client_name'],agent_name=workspace.caller['name'],
                        program='codex',model=model)
    assert not lock.exists()
    assert snapshot(Path(client.isolation))==before


@pytest.mark.parametrize('mutation',[None,'uid','model','task'])
def test_prepared_catalog_profile_reader_uses_real_validation_without_fake_identity(prepared,monkeypatch,mutation):
    import importlib.util
    import sys
    from local_model_auth import intent_digest
    client,workspace,inputs=prepared
    root=Path(client.config['wrapper_root'])
    spec=importlib.util.spec_from_file_location('fixture_installed_catalog_'+uuid.uuid4().hex,
                                               root/'bin/lib/runtime_model_catalog.py')
    catalog=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(catalog)
    # Other installed-client fixtures may have imported a different temporary
    # catalog. Use THIS real installed module for both resolution and reading.
    monkeypatch.setitem(sys.modules,'runtime_model_catalog',catalog)
    policy=catalog.provider_policy('codex')
    assert Path(policy.__file__)==root/'dashboard/codex_models.py'
    monkeypatch.setattr(policy,'resolve_launcher',lambda:policy.LauncherPolicy(
        binary='/fixture/not-executed-codex',version=(0,160,0)))
    monkeypatch.setattr(policy,'_capture',lambda *a,**kw:pytest.fail('native CLI probe'))
    intent=catalog.resolve_catalog_intent({**workspace.caller,'model':'gpt-6-luna','effort':'high'},
                                  workspace.context)
    if mutation=='uid':intent['operator_uid']=os.getuid()+1
    elif mutation=='model':intent['model']['id']='claude-opus-5-5'
    elif mutation=='task':intent['task_sha256']='0'*64
    intent['intent_digest']=intent_digest(intent)
    frame={'kind':client._fixture_api['PROFILE_KIND'],'client_config':str(client.path),
           **client.binding,'configuration_stage':'prepared','launch_intent':intent}
    if mutation:
        # Deliberately corrupt the stored owner as fixture input. write_output
        # is a trusted destination/ownership writer, not an untrusted-input
        # validator; this case tests the actual readonly profile reader.
        client._fixture_api['atomic_json'](client.profile_path,frame)
        before=snapshot(Path(client.isolation))
        with pytest.raises(client._fixture_api['ClientError'],match='OUTPUT_SCHEMA_INVALID'):
            client._fixture_api['RuntimeClient'](client.path,read_only=True)
        assert snapshot(Path(client.isolation))==before
    else:
        client.write_output(client.profile_path,frame,'profile')
        reader=client._fixture_api['RuntimeClient'](client.path,read_only=True)
        before=snapshot(Path(client.isolation))
        checked=launch.admit_local_launch(reader,'codex',workspace.caller['cwd'])
        assert checked['intent']==intent
        assert checked['projection']['execution']['argv']==[
            '--model=gpt-6-luna','-c','model_reasoning_effort="high"']
        assert checked['projection']['inherit_env'] is True
        assert checked['projection']['warm_pool'] is True
        assert reader.identity is None
        assert snapshot(Path(client.isolation))==before
    assert not client.credential.exists() and not client.outputs['registration'].exists()


@pytest.mark.parametrize('mutation,reason',[
    (None,None),('binding','RECOVERY_PENDING_MISMATCH'),('agent-id','OUTPUT_OWNER_CONFLICT'),
    ('token','RECOVERY_CREDENTIAL_MISMATCH')])
def test_readonly_interrupted_registration_recovery_validates_existing_proof_without_lock(operator_server,monkeypatch,mutation,reason):
    import socket
    from runtime_operator import RuntimeOperator
    monkeypatch.setenv('AGENTSTACK_MCP_URL',operator_server['url'])
    operator=RuntimeOperator(operator_server['config_path'])
    operator.prepare('readonly-recovery')
    client=operator.client('readonly-recovery')
    original=client.write_output
    def interrupted(path,value,role):
        original(path,value,role)
        if role=='credential':raise OSError('fixture interrupted credential activation')
    monkeypatch.setattr(client,'write_output',interrupted)
    with pytest.raises(OSError,match='fixture interrupted'):
        client.create('ReadOnlyRecovery','daemon','deterministic')
    pending=operator.api['read_json'](client.outputs['registration'])
    agent_id=pending['row']['agent_id']
    assert client.identity is None and client.credential.exists()
    if mutation=='binding':pending['candidate_generation']='changed-generation'
    elif mutation=='agent-id':pending['row']['agent_id']=agent_id+1000
    elif mutation=='token':pending['registration_token']='different-fixture-token'
    if mutation:operator.api['atomic_json'](client.outputs['registration'],pending)
    lock=client.outputs['operation_lock'];lock.unlink()
    before=snapshot(Path(client.isolation))
    def forbidden(*args,**kwargs):pytest.fail('read-only recovery attempted network')
    with monkeypatch.context() as guard:
        guard.setattr(socket.socket,'connect',forbidden)
        guard.setattr(socket.socket,'connect_ex',forbidden)
        if mutation:
            with pytest.raises(operator.api['ClientError'],match='^'+reason+'$'):
                operator.client('readonly-recovery',read_only=True)
        else:
            reader=operator.client('readonly-recovery',read_only=True)
            assert reader.identity is None and reader._recovery_identity=={'agent_id':agent_id}
    assert not lock.exists()
    assert snapshot(Path(client.isolation))==before


def test_launch_admission_runs_before_capabilities_or_receipt_writes(prepared,monkeypatch):
    client,workspace,inputs,intent = configured(prepared,monkeypatch)
    def forbidden(*args,**kwargs):pytest.fail('R2 effect before native admission')
    monkeypatch.setattr(client,'capabilities',forbidden)
    delivery=client._fixture_api['runtime_delivery'](client)
    monkeypatch.setattr(delivery,'codex_trust',forbidden)
    monkeypatch.setattr(delivery,'occupied_names',forbidden)
    before=snapshot(Path(client.isolation))
    for start in (lambda:delivery.prepare_launch('codex',cwd=workspace.caller['cwd']),
                  lambda:delivery.start('codex',workspace.caller['cwd'],detached=True)):
        with pytest.raises(client._fixture_api['ClientError'],match='ADAPTER_UNSUPPORTED'):start()
        assert snapshot(Path(client.isolation)) == before
    assert not client.outputs['launch'].exists()


@pytest.mark.parametrize('boundary',['initial','retry','execute','provider-exec'])
def test_r2_boundaries_reject_changed_cli_and_keep_receipt(prepared,monkeypatch,boundary):
    original,workspace,inputs=prepared
    # Real isolated Mail registration precedes explicit configure in this
    # fixture. The fake native binary is NEVER executed and proves no capability.
    client=original._fixture_api['RuntimeClient'](original.path)
    client._fixture_api=original._fixture_api
    client.create(workspace.caller['name'],'codex','fixture-glm-model')
    client,workspace,inputs,intent=configured((client,workspace,inputs),monkeypatch)
    delivery=client._fixture_api['runtime_delivery'](client)
    row={'kind':'orrery-global-launch-intent-v1','launch_id':str(uuid.uuid4()),
         'receipt_nonce':'fixture-nonce','phase':'gated','provider':'codex','launch_kind':'startup',
         'cwd':workspace.caller['cwd'],'task_sha256':intent['task_sha256'],
         'intent_digest':intent['intent_digest'],'error':None,'operator_resolution':None}
    if boundary=='provider-exec':
        row.update(phase='exec-unknown',execution={'runner':{'pid':os.getppid(),'process_start':'fixture-parent'},
                                                 'outcome':{'state':'pending','returncode':None}})
        # Unit boundary admission only; this is NOT process-liveness evidence.
        monkeypatch.setitem(delivery.api,'process_start',lambda pid:'fixture-parent')
    delivery.save(client.outputs['launch'],row,'launch_intent')
    request={'launch_id':row['launch_id'],'expected_state_sha256':delivery.state_digest('launch',row['launch_id'],row)}
    binary=Path(inputs['cli']);binary.chmod(0o600);binary.write_text('changed approved driver');binary.chmod(0o500)
    def forbidden(*args,**kwargs):pytest.fail('external effect after changed source')
    monkeypatch.setattr(client,'observe',forbidden)
    monkeypatch.setattr(client,'call',forbidden)
    monkeypatch.setattr(delivery,'occupied_names',forbidden)
    monkeypatch.setattr(delivery,'launch_task',forbidden)
    before=snapshot(Path(client.isolation))
    with pytest.raises(client._fixture_api['ClientError'],match='AUTH_FAILED'):
        if boundary=='initial':
            # A new launch has no previous receipt; no registration/config is
            # inferred from absence. The configured profile is still sole owner.
            client.outputs['launch'].unlink()
            before=snapshot(Path(client.isolation))
            delivery.prepare_launch('codex',cwd=workspace.caller['cwd'])
        elif boundary=='retry':delivery.retry_launch(request)
        elif boundary=='execute':delivery.execute(row['launch_id'],row['receipt_nonce'])
        else:delivery.exec_provider(row['launch_id'],row['receipt_nonce'])
    assert snapshot(Path(client.isolation))==before
    if boundary=='initial':assert not client.outputs['launch'].exists()
    else:assert client._fixture_api['read_json'](client.outputs['launch'])==row
