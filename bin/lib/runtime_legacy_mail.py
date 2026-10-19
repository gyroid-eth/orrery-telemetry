"""Fixed core Mail consumers for legacy configuration and registration.

The fixed consumer supplies the existing shared AgentMailClient/transport.
whois is only an ID lookup, not owner authentication. Do not use its optional
token shaping to infer ownership on the loopback legacy schema.
Only register_configured_child sends a saved new registration, exactly once;
configure and explicit finalization use the read-only owner-auth branch.
"""
import hashlib
import os
from pathlib import Path
import secrets
import sys
import types
import urllib.request

from local_model_auth import (LocalModelError, plain_text, secure_read, source_evidence,
                              check_configuration_evidence)
from runtime_intent_store import checked_intent
from runtime_legacy_context import checked_legacy_context
from runtime_preview_policy import check_preview_request

IDENTITY={'id','name','program','model','project_id'}
SHARED_CLIENT='integrations/codex_app/src/agentstack_codex_app/agent_mail_client.py'


def identity_from_profile(profile):
    """Identity DATA only, not authentication; preserve Mail's raw wire model.

    Native Mail's `model` is a display normalization. `model_raw`, when
    present, is the literal registration. Never resolve that model again, or
    fall back to a matching display value when the raw field is invalid.
    """
    try:
        model=profile['model_raw'] if 'model_raw' in profile else profile['model']
        if (type(profile) is not dict or type(profile.get('id')) is not int or profile['id']<=0
                or type(profile.get('project_id')) is not int or profile['project_id']<=0
                or profile.get('retired_at') is not None
                or any(not plain_text(value) for value in (profile['name'],profile['program'],model))):
            raise ValueError()
        return {'id':profile['id'],'project_id':profile['project_id'],'name':profile['name'],
            'program':profile['program'],'model':model}
    except (KeyError,TypeError,ValueError):
        raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH') from None


def _shared_namespace(script,proof):
    """Compile precisely the pinned core file bytes, never a package/PATH scan.

    The core client is stdlib-only and has no transport calls on import. Its
    dataclass definitions need a temporary module during compilation; no caller
    may select a module name, path or executable. No unverified cache is reused.
    """
    current=source_evidence([str(script)])[str(script)]
    if type(proof) is not dict or proof.get('state')!='present' or current!=proof:
        raise LocalModelError('LOCAL_MODEL_AUTH_FAILED')
    content=secure_read(script,os.getuid())
    if hashlib.sha256(content).hexdigest()!=proof['sha256']:
        raise LocalModelError('LOCAL_MODEL_AUTH_FAILED')
    name='_orrery_legacy_mail_'+secrets.token_hex(16)
    module=types.ModuleType(name);module.__file__=str(script)
    sys.modules[name]=module
    try:exec(compile(content,str(script),'exec'),module.__dict__)
    finally:sys.modules.pop(name,None)
    if source_evidence([str(script)])[str(script)]!=proof:
        raise LocalModelError('LOCAL_MODEL_AUTH_FAILED')
    return module.__dict__


def _fixed_client(target,proof,bearer):
    root=Path(__file__).resolve().parents[2]
    if target['install_root']!=str(root):raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
    if bearer is not None and not plain_text(bearer):raise LocalModelError('LOCAL_MODEL_AUTH_FAILED')
    api=_shared_namespace(root/SHARED_CLIENT,proof)
    transport=api['HttpJsonRpcTransport'](target['mail_url'],bearer_token=bearer,timeout=10.0,allow_redirects=False)
    transport.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),api['_NoRedirect']())
    client=api['AgentMailClient'](transport)
    client._legacy_not_found_error=api['AgentNotFoundError']
    return client


def configured_mail_client(intent,*,_bearer_token=None):
    """Create the fixed core MCP client; no I/O/registration/key lookup here.

    This is only a transport factory, NOT target or launch authentication. The
    consumer must still authenticate its exact parent/child. A bearer, when
    required, comes from that consumer's existing transport selector, never
    ambient token/Keychain discovery or an HTTP argument. None means no header;
    a server rejection stays a rejection without credential fallback.
    """
    try:
        intent=checked_intent(intent)
        check_preview_request(intent,preview_requested=True)
        target=checked_legacy_context(intent['legacy_context'])
        root=Path(__file__).resolve().parents[2]
        if target['install_root']!=str(root):raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
        if _bearer_token is not None and not plain_text(_bearer_token):raise ValueError()
        evidence=intent['configuration_evidence']
        check_configuration_evidence(evidence)
        script=root/SHARED_CLIENT
        proof=evidence['renderer_sources'].get(str(script))
        # Keep the shared redirect refusal AND disable urllib's ambient proxy
        # discovery. No second HTTP implementation or permissive retry route.
        return _fixed_client(target,proof,_bearer_token)
    except LocalModelError:raise
    except Exception:raise LocalModelError('LOCAL_MODEL_AUTH_FAILED') from None


def _authenticate_parent(client,target):
    """Same canonical credential and exact owner identity for fixed consumers."""
    from runtime_legacy_registration import _credential
    parent=target['parent']
    token=secure_read(Path(target['runtime_dir'])/('agent_token_'+parent['name']),os.getuid(),
        mode=0o600,parent_mode=0o700,limit=4096).decode().strip()
    if not _credential(token):raise LocalModelError('LOCAL_MODEL_AUTH_FAILED')
    confirmed=authenticate_existing_owner(client,project_key=target['project_key'],project_id=target['project_id'],
        name=parent['name'],program=parent['program'],model=parent['model'],registration_token=token)
    if confirmed!={**parent,'project_id':target['project_id']}:
        raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
    return confirmed


def configuration_parent_authenticator(target):
    """Fixed operator-only parent observation before a plan exists.

    This configures, not launches: the current installed source is frozen in
    memory before any callback. The resulting plan later pins those same bytes.
    Select the operator's explicit existing identity, never caller.parent as a
    credential lookup. No key lookup, credential fallback or new registration.
    """
    target=checked_legacy_context(target)
    root=Path(__file__).resolve().parents[2]
    parent=target['parent']
    if target['install_root']!=str(root) or os.environ.get('AGENT_NAME')!=parent['name']:
        raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
    script=root/SHARED_CLIENT
    proof=source_evidence([str(script)])[str(script)]
    def authenticate():
        try:
            if os.environ.get('AGENT_NAME')!=parent['name']:raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
            client=_fixed_client(target,proof,None)
            confirmed=_authenticate_parent(client,target)
            # Configure is prepare-before-registration only. This availability
            # read is repeated for apply, not an atomic create-only guarantee.
            # Only the shared client's typed not-found result permits a plan;
            # an existing candidate or an unreadable server is not freshness.
            try:client.whois(project_key=target['project_key'],agent_name=target['name'])
            except client._legacy_not_found_error:pass
            else:raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
            return confirmed
        except LocalModelError:raise
        except Exception:raise LocalModelError('LOCAL_MODEL_AUTH_FAILED') from None
    return authenticate


def registration_finalizer(intent):
    """Default explicit resolve callback: no create/update or launch.

    Build only from the saved target/source, select the current parent before
    credential access, and reauthenticate parent plus child on every attempt.
    Store.finalize invokes this callback only AFTER its expected-digest check
    and compares the returned identity with any saved registration response.
    """
    intent=checked_intent(intent);target=checked_legacy_context(intent['legacy_context'])
    if os.environ.get('AGENT_NAME')!=target['parent']['name']:
        raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
    def query(token):
        if os.environ.get('AGENT_NAME')!=target['parent']['name']:
            raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
        client=configured_mail_client(intent)
        _authenticate_parent(client,target)
        return authenticate_existing_owner(client,project_key=target['project_key'],project_id=target['project_id'],
            name=target['name'],program='claude-code',model=intent['model']['resolved']['model'],
            registration_token=token)
    return query


def authenticate_existing_owner(client,*,project_key,project_id,name,program,model,registration_token):
    """Return identity-only data after the read-only existing_agent_id branch.

    Unknown/no-support never falls back to register/create, token claiming,
    or trusting a whois profile. Never propagate response credentials.
    """
    try:
        if (type(project_id) is not int or project_id<=0
                or any(not plain_text(value) for value in (project_key,name,program,model,registration_token))):
            raise LocalModelError('LOCAL_MODEL_AUTH_FAILED')
        required={'existing_agent_id','registration_token','project_key','name','program','model'}
        if not required<=client._advertised_parameters('register_agent'):
            raise LocalModelError('LOCAL_MODEL_ADAPTER_UNSUPPORTED')
        def matches(value):
            return (value['project_id']==project_id and value['name']==name
                and value['program']==program and value['model']==model)
        profile=identity_from_profile(client._call_tool_object('whois',{'project_key':project_key,'agent_name':name,
            'include_recent_commits':False}))
        if not matches(profile):raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
        # The schema and positive exact ID are mandatory, before this call.
        # app.py's branch returns before _get_or_create_agent/profile writes.
        confirmed=identity_from_profile(client._call_tool_object('register_agent',{'project_key':project_key,
            'name':name,'program':program,'model':model,'registration_token':registration_token,
            'existing_agent_id':profile['id']}))
        if not matches(confirmed) or confirmed['id']!=profile['id']:
            raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
        return {key:confirmed[key] for key in IDENTITY}
    except LocalModelError:
        raise
    except Exception:
        # The server may echo a credential in success OR failure bodies. No raw
        # exception, argument repr or response reaches status/logs/child-state.
        raise LocalModelError('LOCAL_MODEL_AUTH_FAILED') from None


def _registration_projection(intent,caller):
    """Recheck the real common projection, not a native permission approval.

    The fixed consumer uses the stored target and resources; no second model
    resolver, fake global profile, caller-selected install, or CLI execution.
    """
    from runtime_launch_intent import materialize_launch_intent
    from runtime_local_launch import check_caller_intent
    try:
        intent=checked_intent(intent)
        target=checked_legacy_context(intent['legacy_context'])
        if target['install_root']!=str(Path(__file__).resolve().parents[2]):
            raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
        check_caller_intent(caller,intent)
        context={'mode':'legacy','client_root':target['runtime_dir'],'intent_owner':target['owner'],
            'runtime_client':str(Path(target['install_root'])/'bin/lib/runtime_client.py'),
            'operator_uid':os.getuid(),'local_models_root':intent['local_models_root'],
            'execution_policy':intent['execution_policy'],'legacy_context':target,
            'effective_scope':intent['effective_scope'],'approved_settings_digests':intent['scope_evidence'],
            'configuration_evidence':intent['configuration_evidence']}
        auth=intent['model']['resolved']['auth']
        if auth is not None:
            context.update(auth_context=auth['context'],auth_runner=auth['runner'])
            if 'source' in auth:context['auth_source']=auth['source']
        materialize_launch_intent(intent,context,operation='spawn')
        from runtime_legacy_configuration import check_legacy_provider_outputs
        check_legacy_provider_outputs(intent)
        return target
    except LocalModelError:raise
    except Exception:raise LocalModelError('LOCAL_MODEL_CONFIGURATION_CHANGED') from None


def register_configured_child(intent,*,caller,expected_state_digest,candidate_token,
                              preview_requested,_bearer_token=None):
    """Internal preregister consumer; NOT public execution/native admission.

    The outer spawn entry must generate the fresh name, not offer selection of
    existing names. This uses the ordinary legacy register API: whois/register
    races and NULL-owner claims are NOT made atomic/create-only here. Stage1
    additionally uses a fresh dedicated server with no other legitimate writer.

    Persist the original request/credential before exactly one new register;
    then require the read-only existing_agent_id owner branch. Lost responses
    remain unknown, never an automatic resend, repair or new-name fallback.
    """
    from runtime_intent_store import read_owner
    from runtime_legacy_registration import Store,_credential
    try:
        intent=checked_intent(intent)
        check_preview_request(intent,preview_requested=preview_requested)
        if not _credential(candidate_token):raise LocalModelError('LOCAL_MODEL_AUTH_FAILED')
        target=_registration_projection(intent,caller)
        store=Store(target['runtime_dir'],target['name'])
        initial=store.status()
        if initial['state_digest']!=expected_state_digest:
            raise LocalModelError('LOCAL_MODEL_STATE_CHANGED')
        if initial['phase']!='configured':raise LocalModelError('LOCAL_MODEL_PENDING_CONFLICT')
        if read_owner(store.owner)['launch_intent']!=intent:
            raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
        client=configured_mail_client(intent,_bearer_token=_bearer_token)
        required={'project_key','name','program','model','task_description','registration_token','existing_agent_id'}
        if not required<=client._advertised_parameters('register_agent'):
            raise LocalModelError('LOCAL_MODEL_ADAPTER_UNSUPPORTED')
        _authenticate_parent(client,target)
        try:client.whois(project_key=target['project_key'],agent_name=target['name'])
        except client._legacy_not_found_error:pass
        else:raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
        # This is an availability read only, NOT an atomic create-only proof.
        # Ordinary legacy register retains its existing check/use race.
        _registration_projection(intent,caller)
        started=store.begin(expected_digest=expected_state_digest,candidate_token=candidate_token)

        def register(request,token):
            _registration_projection(intent,caller)
            response=client._call_tool_object('register_agent',{**request,'registration_token':token})
            identity=identity_from_profile(response)
            # Native Mail omits this field; old compatible servers can mint a
            # replacement. Only the original registration response may supply
            # it, never the subsequent read-only authentication response.
            adopted=response['registration_token'] if 'registration_token' in response else token
            if not _credential(adopted):raise LocalModelError('LOCAL_MODEL_AUTH_FAILED')
            return {**identity,'registration_token':adopted}

        saved=store.send_once(register,expected_digest=started['state_digest'])

        def authenticate_child(token):
            _registration_projection(intent,caller)
            return authenticate_existing_owner(client,project_key=target['project_key'],project_id=target['project_id'],
                name=target['name'],program='claude-code',model=intent['model']['resolved']['model'],
                registration_token=token)

        return store.finalize(authenticate_child,expected_digest=saved['state_digest'])
    except LocalModelError:raise
    except Exception:raise LocalModelError('LOCAL_MODEL_AUTH_FAILED') from None
