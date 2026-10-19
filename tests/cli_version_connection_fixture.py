"""Comparison-only TEST observation and real R2/auth subprocess entry.

Never native permission enforcement or an adopted CLI. The normal delivery
bootstrap stays disabled; this separate fixture changes one gate target only.
"""
from copy import deepcopy
import hashlib
import importlib
import json
import os
from pathlib import Path
import shlex
import sys


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),
        ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def startup():
    try:
        bootstrap()
    except BaseException:
        os.write(2,b'COMPARISON_FIXTURE_BOOTSTRAP_FAILED\n')
        os._exit(87)


def bootstrap():
    if getattr(bootstrap,'loaded',False):return
    bootstrap.loaded=True
    site=Path(__file__).parent
    config=json.loads((site/'cli-version-fixture.json').read_text())
    assert hashlib.sha256(Path(__file__).read_bytes()).hexdigest()==config['bootstrap_sha256']
    install=Path(config['install'])
    assert install.resolve()==install and install.name=='install'
    assert install.parent.name.startswith('r3-caller-')
    from _orrery_delivery_fixture import _functions
    names=('runtime_native_scope','runtime_intent_store','runtime_launch_intent',
        'runtime_local_launch','runtime_delivery','runtime_client','local_model_auth',
        'process_observation')
    modules=[importlib.import_module(name) for name in names]
    assert all(Path(module.__file__).resolve()==install/'bin/lib'/(module.__name__+'.py')
        for module in modules)
    scope,auth=modules[0],modules[-2]
    assert scope.require_native_binding.__module__=='runtime_native_scope'
    before=_functions(modules)
    def record(event,**fields):
        fd=os.open(config['trace'],os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:os.write(fd,(json.dumps({'event':event,'pid':os.getpid(),**fields})+'\n').encode())
        finally:os.close(fd)
    class TestObservation(scope.NativeConfigurationBinding):
        def observe_configuration(self,intent):
            # Literal fixture version/scope input, NOT CLI interrogation or an
            # interpretation of native permissions. Actual resource checks stay
            # in the common product producer and every R2 boundary.
            value=deepcopy(auth.cli_configuration_snapshot(intent))
            value['cli']={**auth.executable_evidence(value['cli']['path']),
                'origin':auth.cli_installation_origin(value['cli']['path']),
                'version':config['observed_version']}
            observed=value['effective_scope']
            observed['cli_version']=config['observed_version']
            proposal=observed['native_proposal']
            proposal['declared_cli_version']=config['observed_version']
            proposal['requested_scope']['cli_version']=config['observed_version']
            if config.get('scope_change'):
                observed['bash']=[*observed['bash'],'fixture-denied-difference']
            proposal.pop('plan_digest')
            proposal['plan_digest']=digest(proposal)
            record('fixture-observation',provider=intent['provider'])
            return value
    def comparison_only(plan):
        assert plan['declared_cli_version']=='fixture-pinned'
        assert plan['requires_native_validation'] is True and plan['launch_admissible'] is False
        record('comparison-only-native-gate')
        return TestObservation()
    assert type(config['enabled']) is bool
    if config['enabled']:scope.require_native_binding=comparison_only
    after=_functions(modules)
    assert set(before)==set(after)
    changed=sorted(name for name in before if before[name] is not after[name])
    assert changed==(['runtime_native_scope.require_native_binding'] if config['enabled'] else [])
    record('comparison-bootstrap',replacements=changed,replacement_count=len(changed))


def setup(data):
    from runtime_operator import RuntimeOperator
    from runtime_client import RuntimeClient,entry_operation,runtime_delivery
    from runtime_local_configure import plan_local_configuration,apply_local_configuration
    # Existing real preparation APIs; never manufacture successful identities,
    # launch rows or process receipts.
    operator=RuntimeOperator(data['server_config'])
    parent_path=operator.prepare('parent')
    parent=RuntimeClient(parent_path)
    parent.create(data['caller']['parent'],'daemon','deterministic')
    child_path=operator.prepare('child_01')
    child=RuntimeClient(child_path)
    value=plan_local_configuration(child,data['caller'],data['context'],
        python=sys.executable,cli=data['provider_binary'],declared_cli_version='fixture-pinned',
        settings_paths=[data['settings']],skill_sources=[],
        native_sources=[{'path':data['settings'],'layer':'user'}])
    apply_local_configuration(child,value,approved_id=value['plan_id'])
    provider=data['caller']['provider']
    parent.prepare_child({**data['caller'],'child_client_name':'child_01',
        'program':'codex' if provider=='codex' else 'claude-code',
        'workdir':data['caller']['cwd'],'task_file':data['caller']['task'],
        'parsed_tools':{},'prepare_only':True,
        'task_delivery':'mail'})
    child=RuntimeClient(child_path)
    delivery=runtime_delivery(child)
    if provider=='codex':
        sys.path.insert(0,str(Path(data['fixture_root'])/'tests/r3_local_profiles'))
        from caller_trust import prepare_codex_trust
        prepare_codex_trust(delivery)
    delivery.instruction_configure(provider)
    import io
    from contextlib import redirect_stdout
    assert not child.outputs['task'].exists(), 'preregister unexpectedly published the task'
    output=io.StringIO()
    with redirect_stdout(output):
        assert entry_operation('spawn-child',['--context',str(parent_path),
            '--child-client-name','child_01','--prepare-only','--task-file',data['caller']['task']])==0
    published=json.loads(output.getvalue())
    assert published['prepared'] is True and published['runtime_ready'] is False
    assert child.outputs['task'].read_bytes()==Path(data['caller']['task']).read_bytes()
    return {'parent_context':str(parent_path),'child_context':str(child_path),
        'intent':json.loads(child.profile_path.read_text())['launch_intent']}


def launch(data):
    from runtime_client import RuntimeClient,runtime_delivery
    child=RuntimeClient(data['child_context'])
    return runtime_delivery(child).start(data['caller']['provider'],data['caller']['cwd'],
        detached=False,expected_intent_digest=data['intent']['intent_digest'])


def provider(data):
    """Fake provider parses actual projected auth, then invokes real -I helper."""
    import subprocess
    import tomllib
    expected=json.loads(Path(data['expected']).read_text())
    if data['provider']=='codex':
        lines=[]
        for index,arg in enumerate(sys.argv[3:]):
            if arg=='-c':lines.append(sys.argv[3:][index+1])
        config=tomllib.loads('\n'.join(lines))
        auth=config['model_providers']['local_fixture']['auth']
        command=[auth['command'],*auth['args']]
    else:
        args=sys.argv[3:]
        settings=json.loads(Path(args[args.index('--settings')+1]).read_text())
        command=shlex.split(settings['apiKeyHelper'])
    assert command==expected['auth_command'], 'wrong actual projected helper command'
    assert int(os.environ['AGENTSTACK_PROVIDER_PID'])==os.getpid()
    row_path=Path(expected['launch'])
    row=json.loads(row_path.read_text())
    assert row['phase']=='exec-unknown' and row['execution']['runner']['pid']!=os.getpid()
    assert row['intent_digest']==expected['intent_digest']
    assert row['cli_acceptance']['observed_cli']['version']==expected['version']
    assert row['cli_acceptance']['observed_cli']['sha256']==hashlib.sha256(Path(sys.argv[0]).read_bytes()).hexdigest()
    results=[]
    mutation=expected.get('mutation')
    original_receipt=row_path.read_bytes()
    receipt_mutations={'proof-sha','proof-scope','ended-receipt','gated-receipt'}
    if mutation in receipt_mutations:
        invalid=deepcopy(row)
        if mutation=='proof-sha':invalid['cli_acceptance']['proof_sha256']='f'*64
        elif mutation=='proof-scope':
            proof=invalid['cli_acceptance'];proof['scope_sha256']='f'*64
            proof.pop('proof_sha256');proof['proof_sha256']=digest(proof)
        else:invalid['phase']='ended' if mutation=='ended-receipt' else 'gated'
        row_path.write_text(json.dumps(invalid));row_path.chmod(0o600)
    if mutation=='post-exec-bytes':
        binary=Path(sys.argv[0]);binary.chmod(0o700)
        binary.write_bytes(binary.read_bytes()+b'\n# post-exec drift\n');binary.chmod(0o500)
    try:
        for attempt in range(1 if mutation else 2):
            env=dict(os.environ)
            if mutation=='nonce':env['AGENTSTACK_LAUNCH_NONCE']='foreign-fixture-nonce'
            if mutation=='launch-id':env['AGENTSTACK_LAUNCH_ID']='00000000-0000-0000-0000-000000000000'
            if mutation=='provider-pid':env['AGENTSTACK_PROVIDER_PID']=str(row['execution']['runner']['pid'])
            result=subprocess.run(command,capture_output=True,env=env,timeout=15)
            if mutation:
                assert result.returncode==1 and result.stdout==b'' and result.stderr==b'LOCAL_MODEL_AUTH_FAILED\n'
            else:
                assert result.returncode==0 and not result.stderr and result.stdout
            results.append({'returncode':result.returncode,'sha256':hashlib.sha256(result.stdout).hexdigest()})
    finally:
        if mutation in receipt_mutations:
            # Restore only the actual R2 bytes captured above so its real runner
            # can finish. No successful row/session/PID is synthesized by a test.
            row_path.write_bytes(original_receipt);row_path.chmod(0o600)
    Path(expected['result']).write_text(json.dumps({'pid':os.getpid(),
        'launch_id':row['launch_id'],'receipt_nonce':row['receipt_nonce'],
        'cli_acceptance':row['cli_acceptance'],'auth_results':results}))
    # No native session/tool claims; real R2 observes this fake process exit.


def main():
    action=sys.argv[1]
    data=json.loads(Path(sys.argv[2]).read_text())
    result={'setup':setup,'launch':launch,'provider':provider}[action](data)
    if result is not None:print(json.dumps(result,sort_keys=True))


if __name__=='__main__':main()
