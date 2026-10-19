"""Read-only model intent consumption by the existing R2 launch boundaries.

Projection/source evidence is NOT native capability approval. The admission
entry always asks the separate native gate after rebuilding this evidence.
No configure, alias resolution, credential request or ledger is added here.
"""
import json
import os
from pathlib import Path
import runpy
import shutil

from local_model_auth import LocalModelError, secure_read
from runtime_intent_store import checked_intent, read_owner
from runtime_caller_schema import CALLER_FIELDS, OPTIONAL_FIELDS, canonical_caller
from runtime_launch_intent import materialize_launch_intent, model_reference, resolved_model_id
import runtime_native_scope


def validate_caller_request(caller):
    """Shared literal structure/tools admission without lookup or effects."""
    caller = canonical_caller(caller)
    policy = runpy.run_path(str(Path(__file__).resolve().parents[2]/'hooks/child_tools.py'))
    # Validate through existing policy, but keep the literal input rather than
    # silently replacing its field values with a normalized/default selection.
    policy['normalize'](caller['base'], caller['tools'])
    return caller


def check_caller_intent(caller, intent):
    """Compare to an already-checked owner, never resolve or save a model."""
    caller = validate_caller_request(caller)
    expected = {key:value for key,value in intent.items() if key in CALLER_FIELDS|OPTIONAL_FIELDS}
    expected['model'] = model_reference(intent)
    if caller != expected:
        raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
    return caller


def admit_caller_request(client, caller, *, expected_digest=None):
    caller = validate_caller_request(caller)
    result = inspect_local_launch(client,caller['provider'],caller['cwd'],
        requested_model=caller['model'],expected_digest=expected_digest,
        _caller_request=caller,_native_admission=True)
    if result is None:
        raise LocalModelError('LOCAL_MODEL_INTENT_OWNER_REQUIRED')
    return result


def inspect_local_launch(client, provider, cwd, *, expected_digest=None, operation='spawn',
                         provider_session_id=None, require_receipt=False, requested_model=None,
                         expected_task_sha256=None, _caller_request=None,
                         _native_admission=False, _cli_acceptance=None):
    """Return a checked additive projection, never a launch authorization.

    Only the bound fixed profile can select local configuration. A missing
    intent cannot silently turn a local metadata selector into a cloud launch.
    A receipt digest pins retries/exec to the original intent without copying it.
    """
    if client.mode != 'global':
        raise LocalModelError('LOCAL_MODEL_ADAPTER_UNSUPPORTED')
    owner = client.profile_path
    frame = {}
    if owner.exists() or owner.is_symlink():
        client.validate_output(owner,'profile')
        frame = read_owner(owner)
    value = frame.get('launch_intent')
    if value is None:
        metadata = client.registration_metadata() if hasattr(client,'registration_metadata') else {}
        models = [requested_model,frame.get('model'),(client.identity or {}).get('model'),metadata.get('model')]
        if expected_digest is not None or any(isinstance(model,str) and model.startswith('local:') for model in models):
            raise LocalModelError('LOCAL_MODEL_INTENT_OWNER_REQUIRED')
        return None
    intent = checked_intent(value)
    if not isinstance(intent.get('model'),dict) or intent['model'].get('kind') not in ('local-profile','catalog'):
        raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
    try:
        caller = {key:value for key,value in intent.items() if key in CALLER_FIELDS|OPTIONAL_FIELDS}
        caller['model'] = model_reference(intent)
        canonical_caller(caller)
        if intent['model']['kind']=='local-profile' and (caller.get('effort') not in (None,'') or caller.get('strict_output_cap',False)):
            raise LocalModelError('LOCAL_MODEL_ADAPTER_UNSUPPORTED')
    except (KeyError,TypeError,ValueError):raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED') from None
    if require_receipt and expected_digest is None:
        raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
    if expected_digest is not None and intent['intent_digest'] != expected_digest:
        raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
    if require_receipt and expected_task_sha256 != intent.get('task_sha256'):
        raise LocalModelError('LOCAL_MODEL_TASK_CHANGED')
    if requested_model is not None and requested_model != model_reference(intent):
        raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
    if (intent.get('provider') != provider or intent.get('cwd') != str(cwd)
            or (client.identity and intent.get('name') != client.identity.get('name'))):
        raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
    if _caller_request is not None:check_caller_intent(_caller_request,intent)
    try:
        if intent['model']['kind']=='catalog':
            context={'mode':'global','client_root':str(client.client_root),'operator_uid':os.getuid()}
            projection=materialize_launch_intent(intent,context,operation=operation,
                                                provider_session_id=provider_session_id)
            return {'intent':intent,'projection':projection}
        evidence = intent['configuration_evidence']
        scope = intent['effective_scope']
        context = {'mode':'global','client_root':str(client.client_root),
            'intent_owner':str(owner),'operator_uid':os.getuid(),
            'runtime_client':str(Path(client.config['wrapper_root'])/'bin/lib/runtime_client.py'),
            'local_models_root':intent['local_models_root'],
            'effective_scope':scope,'approved_settings_digests':intent['scope_evidence'],
            'configuration_evidence':evidence}
        auth = intent['model']['resolved']['auth']
        if auth is not None:
            context.update(auth_context=auth['context'],auth_runner=auth['runner'])
            if 'source' in auth:context['auth_source'] = auth['source']
        # Select through R2's existing binary input, but execute only the exact
        # approved canonical path after comparison. No version probe is run.
        selected = os.environ.get('AGENTSTACK_'+provider.upper()+'_BIN') or provider
        actual = shutil.which(selected)
        if actual is None or str(Path(actual).resolve()) != evidence['cli']['path']:
            raise LocalModelError('LOCAL_MODEL_CLI_CHANGED')
        projection = materialize_launch_intent(intent,context,operation=operation,
            provider_session_id=provider_session_id,_native_admission=_native_admission,
            _cli_acceptance=_cli_acceptance)
        home = client.runtime_dir/'provider-home'/provider
        if provider == 'claude':
            settings = home/'local-model-settings.json'
            observed = json.loads(secure_read(settings,os.getuid(),mode=0o600,parent_mode=0o700))
            if observed != projection['provider_config']['settings']:
                raise LocalModelError('LOCAL_MODEL_CONFIGURATION_CHANGED')
        return {'intent':intent,'projection':projection,'binary':evidence['cli']['path'],
                'home':str(home),'settings':str(home/'local-model-settings.json') if provider=='claude' else None}
    except (KeyError,TypeError,ValueError,OSError):
        raise LocalModelError('LOCAL_MODEL_CONFIGURATION_CHANGED') from None


def admit_local_launch(client, provider, cwd, **options):
    """The ordinary launch gate; unverified proposals MUST remain unsupported."""
    return inspect_local_launch(client,provider,cwd,_native_admission=True,**options)


def admit_local_registration(client, name, program, model, *, metadata=False):
    """Shared pre-network check, including preparation with no agent ID yet."""
    frame = read_owner(client.profile_path) if client.profile_path.exists() or client.profile_path.is_symlink() else {}
    intent = frame.get('launch_intent')
    local = isinstance(model,str) and model.startswith('local:')
    if intent is None and not local:return None
    if intent is None:raise LocalModelError('LOCAL_MODEL_INTENT_OWNER_REQUIRED')
    intent = checked_intent(intent)
    if intent.get('name') != name:raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
    if metadata:
        # Internal retained metadata stores the real ID, not the selector.
        # This is not accepted as an alias for explicit caller model input.
        try:
            if model != resolved_model_id(intent):
                raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
            model = model_reference(intent)
        except (KeyError,TypeError):raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED') from None
    from runtime_caller_schema import PROVIDERS
    provider = PROVIDERS.get(program)
    if provider is None:raise LocalModelError('LOCAL_MODEL_ADAPTER_UNSUPPORTED')
    return admit_local_launch(client,provider,intent.get('cwd'),requested_model=model)


_RUNTIME_KEYS = frozenset({'AGENTSTACK_CLIENT_CONFIG','AGENTSTACK_RUNTIME_MODE',
    'AGENTSTACK_LAUNCH_ID','AGENTSTACK_LAUNCH_NONCE','AGENTSTACK_RUNTIME_DIR',
    'AGENTSTACK_MCP_URL','AGENT_NAME','AGENTSTACK_HOME','AGENTSTACK_AGENT_ID'})


def local_environment(checked, runtime, *, provider_pid=None):
    """Same minimal environment for runner child and exec, not ambient merging.

    Runtime fields are derived by the bound R2 consumer, not caller input.
    Provider PID is added only by the exec child which becomes the provider.
    """
    if set(runtime) != _RUNTIME_KEYS or any(not isinstance(value,str) for value in runtime.values()):
        raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
    home = checked['home']
    env = {'HOME':home,'CODEX_HOME':home,'CLAUDE_CONFIG_DIR':home,
           'PATH':'/usr/bin:/bin','LANG':'C',**checked['projection']['execution']['env'],**runtime}
    env['AGENTSTACK_'+checked['intent']['provider'].upper()+'_BIN'] = checked['binary']
    if provider_pid is not None:
        if type(provider_pid) is not int or provider_pid <= 0:
            raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
        env['AGENTSTACK_PROVIDER_PID'] = str(provider_pid)
    return env


def local_provider_command(checked, row, *, proxy, plugin_id=None, extra_servers=None,
                           tool_flags=(), prompt='', tool_prompt='', provider_session_id=None,
                           headless=False):
    """Compose only the existing native envelope and the common projection.

    This pure function is not an admission entry. R2 must call the native gate
    and validate instructions, trust, tools and resume evidence before using it.
    Claude accepts the MCP JSON inline so ordinary local launch has no derived
    config write. Native compatibility is still an unverified capability.
    """
    provider = checked['intent']['provider']
    if row['provider'] != provider or row['cwd'] != checked['intent']['cwd']:
        raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
    binary = checked['binary']
    if provider == 'codex':
        if not isinstance(plugin_id,str) or not plugin_id:
            raise LocalModelError('LOCAL_MODEL_ADAPTER_UNSUPPORTED')
        args = [binary,'-C',row['cwd'],'-c','check_for_update_on_startup=false',
                '-c','mcp_servers.orrery-mail.command='+json.dumps(proxy[0]),
                '-c','mcp_servers.orrery-mail.args='+json.dumps(proxy[1:]),
                '-c','plugins.'+json.dumps(plugin_id)+'.mcp_servers.agentstack.enabled=false']
    elif provider == 'claude':
        args = [binary,'--mcp-config',json.dumps({'mcpServers':{**(extra_servers or {}),
            'orrery-mail':{'command':proxy[0],'args':proxy[1:]}}},sort_keys=True),'--strict-mcp-config']
    else:raise LocalModelError('LOCAL_MODEL_ADAPTER_UNSUPPORTED')
    if row['launch_kind'] == 'resume':
        if not provider_session_id:raise LocalModelError('PROVIDER_SESSION_REQUIRED')
        args += ((['exec','resume',provider_session_id] if headless else ['resume',provider_session_id])
                 if provider=='codex' else ['--resume',provider_session_id])
    if provider == 'claude':args += ['--settings',checked['settings']]
    args += checked['projection']['execution']['argv'] + list(tool_flags)
    if prompt and row['launch_kind']=='startup':
        args.append(prompt+('\n\n'+tool_prompt if tool_prompt else ''))
    return args
