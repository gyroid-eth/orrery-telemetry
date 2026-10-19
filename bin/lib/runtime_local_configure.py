"""Private explicit-configure producer/apply, never an ordinary launch path.

The bound client supplies the only owner, fixed HOME and install root. A common
provider plan must already supply the approved effective scope and complete
settings source set. Resource fingerprints do NOT prove native enforcement;
native composition/admission remains mandatory before launch can be enabled.
No credentials, native CLI, transport or model requests are executed here.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys

from local_model_auth import (LocalModelError, canonical_digest, check_auth_source,
                              check_configuration_evidence, cli_installation_origin, intent_digest, secure_read,
                              validate_auth_context)
from runtime_configure_ownership import plan_outputs, apply_outputs
from runtime_intent_store import check_idle, checked_intent, read_owner
from runtime_launch_intent import (_resolution_inputs, _new_intent,
                                  _build_provider_projection, resolve_launch_intent)
import runtime_model_resources as resources
from runtime_native_scope import plan_native_scope

CHANGED = 'LOCAL_MODEL_CONFIGURE_PLAN_CHANGED'


def _owner_bytes(client):
    path = client.profile_path
    if path.exists() or path.is_symlink():
        return secure_read(path,os.getuid(),mode=0o600,parent_mode=0o700)
    return None


def _fingerprint(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _context_bytes(client):
    """Fixed context snapshot; transport adapters must not replace the owner."""
    if client.mode=='legacy':return client.context_bytes()
    return secure_read(client.path,os.getuid(),mode=0o600,parent_mode=0o700)


def _fixed_context(client,value):
    """Existing global derivation, extracted before adding a legacy adapter."""
    if client.mode=='legacy':return client.fixed_context(value)
    if client.mode != 'global':raise LocalModelError('LOCAL_MODEL_ADAPTER_UNSUPPORTED')
    context=deepcopy(value)
    if context.get('operator_uid') != os.getuid():raise LocalModelError(CHANGED)
    context.update(mode='global',client_root=str(client.client_root),intent_owner=str(client.profile_path),
                   runtime_client=str(Path(client.config['wrapper_root'])/'bin/lib/runtime_client.py'))
    return context


def _provider_home(client,provider):
    if client.mode=='legacy':return client.provider_home(provider)
    return client.runtime_dir/'provider-home'/provider


def _framework_settings(client, python):
    if client.mode=='legacy':return client.framework_settings(python)
    from runtime_delivery import claude_hook_settings
    return claude_hook_settings(sys.executable,
        Path(client.config['wrapper_root'])/'bin/lib/runtime_client.py',client.path)


def _frame(client, intent, previous):
    if client.mode=='legacy':return client.profile_frame(intent,previous)
    if previous:
        return {**previous,'launch_intent':intent}
    frame = {'kind':'orrery-global-client-profile-v1','client_config':str(client.path),
             **client.binding,'launch_intent':intent}
    if client.identity:
        frame.update({key:value for key,value in client.identity.items()
                      if key not in ('window_row_id','window_uuid')})
    else:
        # A typed pre-registration stage in the SAME profile slot. Never
        # manufacture an agent ID/credential merely to validate the owner.
        frame['configuration_stage'] = 'prepared'
    return frame


def _data(client, inputs):
    """Rebuild exact data/output bytes from admitted inputs, not plan assertions."""
    context = _fixed_context(client,inputs['context'])
    caller,profile,scope,scope_hashes = _resolution_inputs(inputs['caller'],context)
    if client.identity and client.identity.get('name') != caller['name']:
        raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
    if inputs['declared_cli_version'] != scope['cli_version']:raise LocalModelError(CHANGED)
    home = _provider_home(client,caller['provider'])
    install = resources.canonical_path(client.config['wrapper_root'])
    sources = resources.settings_sources(inputs['settings_paths'])
    if not set(scope['settings_sources']) <= set(sources):raise LocalModelError(CHANGED)
    if {row['path'] for row in inputs['native_sources']} != set(sources):raise LocalModelError(CHANGED)
    native = plan_native_scope(caller['provider'],scope,inputs['native_sources'])
    scope = {**scope,'native_proposal':native}
    if caller['provider'] == 'claude':
        scope['framework_settings'] = _framework_settings(client, inputs['python'])
    context['effective_scope'] = scope
    skills = resources.skill_outputs(inputs['skill_sources'],home/'skills')
    renderer_names = (
        'runtime_local_configure.py','runtime_native_scope.py','runtime_launch_intent.py','runtime_provider_projection.py',
        'runtime_delivery.py','runtime_client.py','runtime_local_launch.py',
        'runtime_model_resources.py','local_model_auth.py','local_model_profiles.py',
        'runtime_caller_schema.py','runtime_intent_store.py','runtime_model_catalog.py',
        'instruction_blocks.py','process_observation.py')
    if client.mode=='legacy':
        renderer_names += ('runtime_legacy_context.py','runtime_preview_policy.py',
            'runtime_legacy_configuration.py','runtime_legacy_preview_cli.py',
            'runtime_legacy_registration.py','runtime_legacy_mail.py',
            'runtime_legacy_execution.py','runtime_legacy_preview.py','runtime_configure_ownership.py',
            'runtime_legacy_proxy.py','runtime_legacy_worker.py')
    # Private library callers may load the producer separately from the fixed
    # installed client. Pin BOTH active renderers and the fixed deployed set;
    # do not substitute one for the other or relax the fixed target check.
    fixed_library = Path(context['runtime_client']).parent
    renderer_paths = list(dict.fromkeys([str(directory/name)
        for directory in (Path(__file__).parent,fixed_library) for name in renderer_names]))
    renderer_paths = list(dict.fromkeys(renderer_paths+[str(root/'hooks/child_tools.py')
        for root in (Path(__file__).resolve().parents[2],fixed_library.parents[1])]))
    if client.mode=='legacy':
        from runtime_legacy_configuration import legacy_proxy_sources
        renderer_paths += [str(client.path),str(client.reader),str(install/
            'integrations/codex_app/src/agentstack_codex_app/agent_mail_client.py'),
            str(install/'claude/CLAUDE.md'),str(install/'hooks/check-reservation-conflicts.py'),
            str(install/'hooks/reservation-patterns.py')]
        renderer_paths=list(dict.fromkeys(renderer_paths+legacy_proxy_sources(install)))
    context['configuration_evidence'] = {
        'contract_version':1,
        'cli':{**resources.executable_evidence(inputs['cli'],system_owner=True),
               'origin':cli_installation_origin(inputs['cli']),
               'declared_version':inputs['declared_cli_version']},
        'settings_sources':sources,'skill_sources':skills['source_evidence'],
        'renderer_sources':resources.settings_sources(renderer_paths),'requires_native_validation':True}
    if client.mode=='legacy':
        context['configuration_evidence']['runtime_python']=resources.executable_evidence(
            inputs['python'],system_owner=True)
    check_configuration_evidence(context['configuration_evidence'])
    source = resources.auth_source(profile,caller['provider'],inputs['auth_dependencies'])
    auth,outputs = None,list(skills['outputs'])
    if client.mode=='legacy':
        from runtime_legacy_configuration import legacy_instruction_output,legacy_proxy_output
        outputs.append(legacy_instruction_output(client.target,
            context['configuration_evidence']['renderer_sources']))
        outputs.append(legacy_proxy_output(client.target,scope,
            context['configuration_evidence']['runtime_python'],
            context['configuration_evidence']['renderer_sources']))
    if source is not None:
        validate_auth_context(context['auth_context'])
        if context['auth_context']['cwd'] != str(context['local_models_root']):
            raise LocalModelError('LOCAL_MODEL_AUTH_FAILED')
        runner = resources.runner_output(install,inputs['python'])
        context.update(auth_runner=runner['runner'],auth_source=source)
        check_auth_source(source,profile[caller['provider']]['auth_command'],os.getuid())
        auth = {'context':deepcopy(context['auth_context']),'runner':runner['runner'],'source':source}
        outputs.append(runner['output'])
    else:
        context.pop('auth_source',None)
        context.pop('auth_runner',None)
    intent = _new_intent(caller,context,profile,scope,scope_hashes,auth)
    intent['revision'] = inputs['revision']
    intent['task_sha256'] = hashlib.sha256(secure_read(caller['task'],os.getuid())).hexdigest()
    intent['intent_digest'] = intent_digest(intent)
    if caller['provider'] == 'claude':
        projection = _build_provider_projection(intent,context)
        outputs.append({'path':str(home/'local-model-settings.json'),'kind':'provider-settings','mode':0o600,
                        'content':json.dumps(projection['provider_config']['settings'],sort_keys=True,ensure_ascii=False)})
    # Own derived outputs only. Shared normal config/instruction files are NOT
    # adopted, copied or overwritten by this producer.
    managed = {output['path'] for output in outputs}
    if managed & (set(sources)|set(scope_hashes)):raise LocalModelError(CHANGED)
    return intent,context,outputs,[install/'libexec/local-model-auth',home]


def plan_local_configuration(client, caller, context, *, python, cli, declared_cli_version,
                             settings_paths, skill_sources, native_sources, auth_dependencies=None):
    """Read-only transient plan; no manifest/owner/lock writes or native probes."""
    authorized=client.authenticate_target() if client.mode=='legacy' else None
    client.validate_output(client.profile_path,'profile')
    context_before = _context_bytes(client)
    if client.mode=='legacy' and context_before!=authorized:raise LocalModelError(CHANGED)
    before = _owner_bytes(client)
    prior = json.loads(before) if before is not None else {}
    old = checked_intent(prior['launch_intent']) if 'launch_intent' in prior else None
    inputs = {'caller':deepcopy(caller),'context':deepcopy(context),'python':str(python),'cli':str(cli),
              'declared_cli_version':declared_cli_version,'settings_paths':list(settings_paths),
              'native_sources':deepcopy(native_sources),
              'skill_sources':list(skill_sources),'auth_dependencies':list(auth_dependencies or []),
              'revision':old['revision'] if old else 1}
    intent,_,outputs,roots = _data(client,inputs)
    if old is not None and intent != old:
        inputs['revision'] += 1
        intent,_,outputs,roots = _data(client,inputs)
    if old != intent:check_idle(client.profile_path,client.mode)
    output_plan = plan_outputs(client.config['wrapper_root'],outputs,allowed_roots=roots)
    if (_owner_bytes(client) != before or
            _context_bytes(client) != context_before):
        raise LocalModelError(CHANGED)
    value = {'kind':'orrery-local-configure-plan-v1','owner':str(client.profile_path),
             'client_context_sha256':_fingerprint(context_before),
             'owner_before_sha256':_fingerprint(before),
             'owner_after_digest':canonical_digest(_frame(client,intent,prior)),
             'inputs':inputs,'intent':intent,'output_plan':output_plan}
    value['plan_id'] = canonical_digest(value)
    return value


def apply_local_configuration(client, plan, *, approved_id, checkpoint=None):
    """Explicit same-plan apply under C/fence, then the existing manifest mutex."""
    if not approved_id:raise LocalModelError('LOCAL_MODEL_CONFIGURE_CONFIRMATION_REQUIRED')
    if (plan.get('kind') != 'orrery-local-configure-plan-v1' or plan.get('plan_id') != approved_id
            or canonical_digest({k:v for k,v in plan.items() if k!='plan_id'}) != approved_id
            or plan.get('owner') != str(client.profile_path)):
        raise LocalModelError(CHANGED)
    if client.mode=='legacy':
        if _fingerprint(_context_bytes(client)) != plan['client_context_sha256']:
            raise LocalModelError(CHANGED)
        if _fingerprint(client.authenticate_target())!=plan['client_context_sha256']:
            raise LocalModelError(CHANGED)
    with client.operation_mutex(), client.fence():
        if _fingerprint(_context_bytes(client)) != plan['client_context_sha256']:
            raise LocalModelError(CHANGED)
        before = _owner_bytes(client)
        previous = json.loads(before) if before is not None else {}
        completed = canonical_digest(previous) == plan['owner_after_digest']
        # A lost-return replay must not repair outputs beneath a running or
        # unresolved launch merely because its intent was already saved.
        check_idle(client.profile_path,client.mode)
        if not completed:
            if _fingerprint(before) != plan['owner_before_sha256']:raise LocalModelError(CHANGED)
        intent,context,outputs,roots = _data(client,plan['inputs'])
        original = [{key:value for key,value in output.items() if key not in ('before','after')}
                    for output in plan['output_plan']['outputs']]
        if (intent != plan['intent'] or outputs != original
                or plan['output_plan']['allowed_roots'] != list(map(str,roots))
                or canonical_digest(_frame(client,intent,previous)) != plan['owner_after_digest']):
            raise LocalModelError(CHANGED)
        result = apply_outputs(client.config['wrapper_root'],plan['output_plan'],
                               approved_id=plan['output_plan']['plan_id'],checkpoint=checkpoint)
        # Publication is not a reason to bypass the NORMAL resolver's checks.
        resolved = resolve_launch_intent(plan['inputs']['caller'],context)
        resolved.update(revision=intent['revision'],task_sha256=intent['task_sha256'])
        resolved['intent_digest'] = intent_digest(resolved)
        if resolved != intent:raise LocalModelError(CHANGED)
        if checkpoint:checkpoint('before-intent-save')
        if _owner_bytes(client) != before:raise LocalModelError(CHANGED)
        if not completed:client.write_output(client.profile_path,_frame(client,intent,previous),'profile')
        return {'ok':True,'configured':True,'changed':result['changed'] or not completed,
                'plan_id':approved_id,'intent_digest':intent['intent_digest'],
                'requires_native_validation':True,'launch_admissible':False}
