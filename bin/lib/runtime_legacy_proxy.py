"""Fixed legacy Mail-only proxy consumer; no public exec/native admission.

The core MCP server/transport are reused from frozen approved source bytes.
Each tool binds the same registered owner and active real helper/Q/P process
chain. No IdentityStore, snapshot file, token argument or credential fallback.
The stdio/launch entry is connected separately; this factory alone is not a
working preview child or proof of native permissions/isolation.
"""
import builtins
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import secrets
import sys
import time
import types
import unicodedata
import urllib.parse
import urllib.request

from local_model_auth import LocalModelError, secure_read, source_evidence

FAILED='LOCAL_MODEL_AUTH_FAILED'


def _frozen_files(intent,paths):
    records={path:intent['configuration_evidence']['renderer_sources'][path] for path in paths}
    if (any(type(row) is not dict or row.get('state')!='present' for row in records.values())
            or source_evidence(paths)!=records):raise LocalModelError(FAILED)
    code={}
    for path in paths:
        content=secure_read(path,os.getuid())
        if hashlib.sha256(content).hexdigest()!=records[path]['sha256']:raise LocalModelError(FAILED)
        code[path]=content
    if source_evidence(paths)!=records:raise LocalModelError(FAILED)
    return code,records


def _auth_contract(intent,root):
    """Use the source-pinned auth core/closed readers, not a product cache."""
    path=str(root/'bin/lib/local_model_auth.py')
    code,records=_frozen_files(intent,[path])
    module=types.ModuleType('_orrery_preview_auth_'+secrets.token_hex(16))
    module.__file__=path
    exec(compile(code[path],path,'exec'),module.__dict__)
    if source_evidence([path])!=records:raise LocalModelError(FAILED)
    module.check_configuration_evidence(intent['configuration_evidence'])
    api=module._legacy_contract(intent,_fixed_root=root)
    return module,api


def _proxy_contract(intent,root,api):
    paths=api['runtime_legacy_configuration'].legacy_proxy_sources(root)
    code,records=_frozen_files(intent,paths)
    modules={};package='_orrery_preview_proxy_'+secrets.token_hex(16)
    original=builtins.__import__
    def fixed_import(name,globals=None,locals=None,fromlist=(),level=0):
        if level:
            if level!=1 or name not in modules:raise LocalModelError(FAILED)
            return modules[name]
        if name.startswith(('agentstack','runtime_','local_model_')):raise LocalModelError(FAILED)
        return original(name,globals,locals,fromlist,level)
    # Freeze EVERY file before executing any. This order is the closed import
    # graph of the existing stdlib proxy; its package __init__ is not executed.
    for path in paths:
        name=Path(path).stem;unique=package+'_'+name
        module=types.ModuleType(unique);module.__file__=path;module.__package__=package
        module.__builtins__={**vars(builtins),'__import__':fixed_import}
        sys.modules[unique]=module  # dataclass definitions only, never a cache
        try:exec(compile(code[path],path,'exec'),module.__dict__)
        finally:sys.modules.pop(unique,None)
        modules[name]=module
    if source_evidence(paths)!=records:raise LocalModelError(FAILED)
    return modules


def _process_contract(intent,root):
    path=str(root/'bin/lib/process_observation.py')
    code,records=_frozen_files(intent,[path])
    module=types.ModuleType('_orrery_preview_process_'+secrets.token_hex(16));module.__file__=path
    exec(compile(code[path],path,'exec'),module.__dict__)
    if source_evidence([path])!=records:raise LocalModelError(FAILED)
    return module


def _reservation_contract(intent,root):
    """Freeze BOTH shared helpers before executing either; no legacy HTTP."""
    paths=[str(root/'hooks'/name) for name in
        ('check-reservation-conflicts.py','reservation-patterns.py')]
    code,records=_frozen_files(intent,paths)
    modules=[]
    for path in paths:
        module=types.ModuleType('_orrery_preview_reservations_'+secrets.token_hex(16))
        module.__file__=path
        exec(compile(code[path],path,'exec'),module.__dict__)
        modules.append(module)
    if source_evidence(paths)!=records:raise LocalModelError(FAILED)
    return modules[0].conflicting_owners,modules[1].patterns_overlap


def _context(intent,root):
    target=intent['legacy_context']
    value={'mode':'legacy','client_root':target['runtime_dir'],'intent_owner':target['owner'],
        'runtime_client':str(root/'bin/lib/runtime_client.py'),'operator_uid':os.getuid(),
        'local_models_root':intent['local_models_root'],'execution_policy':intent['execution_policy'],
        'legacy_context':target,'effective_scope':intent['effective_scope'],
        'approved_settings_digests':intent['scope_evidence'],'configuration_evidence':intent['configuration_evidence']}
    auth=intent['model']['resolved']['auth']
    if auth is not None:
        value.update(auth_context=auth['context'],auth_runner=auth['runner'])
        if 'source' in auth:value['auth_source']=auth['source']
    return value


def _active_context(intent):
    """PRIVATE shared proof for proxy/hooks, never a public owner projection.

    The returned closures re-read the same active owner/processes. Credentials
    stay internal, and creating this context sends no HTTP or tool request.
    """
    try:
        root=Path(__file__).resolve().parents[2]
        if intent['legacy_context']['install_root']!=str(root):raise LocalModelError(FAILED)
        auth,api=_auth_contract(intent,root)
        intent=api['runtime_intent_store'].checked_intent(intent)
        target=api['runtime_legacy_context'].checked_legacy_context(intent['legacy_context'])
        context=_context(intent,root)
        def snapshot():
            # Real shared validation, NOT native permission approval. Same
            # profile/task/runner/source/scope guards as registration/launch.
            api['runtime_launch_intent'].materialize_launch_intent(intent,context,operation='spawn')
            return api['runtime_legacy_execution'].active_execution_snapshot(target['owner'],intent)
        core=_proxy_contract(intent,root,api)
        process=_process_contract(intent,root)
        def validate():
            before=snapshot();row=before['row']
            if (os.environ.get('PATH')!='/usr/bin:/bin'
                    or not process.active_provider_binding(os.getpid(),row['execution']['provider'],
                        row['execution']['runner'],os.getuid())):raise LocalModelError(FAILED)
            after=snapshot()
            if auth.canonical_digest(before)!=auth.canonical_digest(after):raise LocalModelError(FAILED)
            return after
        def binding_identity(value):
            row=value['row']
            return auth.canonical_digest({'owner':{k:v for k,v in value['owner'].items() if k!='local_execution'},
                'launch_id':row['launch_id'],'receipt_nonce':row['receipt_nonce'],
                'runner':row['execution']['runner'],'provider':row['execution']['provider']})
        initial=validate();pinned=binding_identity(initial)
        return auth,api,core,target,validate,binding_identity,initial,pinned
    except Exception:raise LocalModelError(FAILED) from None


def _mail_transport(core,target,bearer_token=None):
    mail_api=core['agent_mail_client']
    transport=mail_api.HttpJsonRpcTransport(target['mail_url'],bearer_token=bearer_token,
        timeout=10.0,allow_redirects=False)
    transport.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),mail_api._NoRedirect())
    return transport


def configured_preview_proxy(intent,*,_bearer_token=None):
    """Internal fixed consumer. No registration/provider/key execution here."""
    try:
        auth,api,core,target,validate,binding_identity,initial,pinned=_active_context(intent)
        mail_api=core['agent_mail_client']
        if _bearer_token is not None and not auth.plain_text(_bearer_token):raise LocalModelError(FAILED)
        transport=_mail_transport(core,target,_bearer_token)
        client=mail_api.AgentMailClient(transport)
        proxy_api=core['mcp_server']
        class NoIdentityStore:
            def resolve(self,*args):raise proxy_api.ProxyError(FAILED)
            def load_owner_token(self,*args):raise proxy_api.ProxyError(FAILED)
        class NoBridgeSnapshot:
            def get(self,external_id):
                expected=core['identity_store'].external_id_for('direct-'+target['name'])
                if external_id!=expected:raise proxy_api.ProxyError(FAILED)
                return None
        class GuardedProxy(proxy_api.AgentStackProxy):
            def _resolve(self,session_id,agent_id):
                try:
                    current=validate()
                    binding,token=super()._resolve(session_id,agent_id)
                    if (binding_identity(current)!=pinned or binding!=fixed_binding
                            or token!=current['owner']['registration_token']):raise LocalModelError(FAILED)
                    return binding,token
                except Exception:raise proxy_api.ProxyError(FAILED) from None
        proxy=GuardedProxy(NoIdentityStore(),NoBridgeSnapshot(),client,bootstrap_wait_seconds=0)
        fixed_binding=proxy.bind_direct(agent_name=target['name'],project_key=target['project_key'],
            owner_token=initial['owner']['registration_token'],program='claude-code',parent_agent=target['parent']['name'])
        return proxy_api.StdioMcpServer(proxy)
    except Exception:raise LocalModelError(FAILED) from None


def guard_preview_edit(intent,operation,payload):
    """Same active owner/session and legacy renew/conflict rules; no release.

    Routine missing leases, service failure or session end must stop an edit.
    Post-edit deliberately keeps the lease until explicit release/expiry/join.
    """
    failure='LOCAL_MODEL_RESERVATION_FAILED'
    try:
        event={'pre-edit':'PreToolUse','post-edit':'PostToolUse'}[operation]
        if (type(payload) is not dict or not isinstance(payload.get('session_id'),str)
                or payload.get('cwd')!=intent['cwd']
                or ('hook_event_name' in payload and payload['hook_event_name']!=event)):
            raise LocalModelError(failure)
        auth,_,core,target,validate,binding_identity,_,pinned=_active_context(intent)
        def checked():
            current=validate();row=current['row']
            if (binding_identity(current)!=pinned or row['phase']!='running'
                    or row['session_receipt']!={'session_id':payload['session_id'],'phase':'started'}):
                raise LocalModelError(failure)
            return current
        checked()
        if operation=='post-edit':return {'ok':True,'released':0}
        tool_input=payload.get('tool_input',{})
        raw=tool_input.get('file_path',tool_input.get('path',tool_input.get('notebook_path')))
        if not auth.plain_text(raw):raise LocalModelError(failure)
        home=Path(target['owner']).with_suffix('.preview-home')
        path=home/raw[2:] if raw.startswith('~/') else Path(raw)
        if not path.is_absolute():path=Path(intent['cwd'])/path
        path=path.resolve();project=Path(target['project_key']).resolve()
        if not path.is_relative_to(project):return {'ok':True,'protected':False,'renewed':0}
        paths=[unicodedata.normalize('NFC',str(path.relative_to(project))),
               unicodedata.normalize('NFC',str(path))]
        root=Path(target['install_root'])
        reader,matcher=_reservation_contract(intent,root)
        transport=_mail_transport(core,target)
        def tracked(request):
            checked();response=transport(request);checked();return response
        client=core['agent_mail_client'].AgentMailClient(tracked)
        def renew():
            current=checked()
            result=client.renew_reservations(project_key=target['project_key'],agent_name=target['name'],
                paths=paths,extend_seconds=900,registration_token=current['owner']['registration_token'])
            count=result.get('renewed')
            if type(count) is not int or count<0:raise LocalModelError(failure)
            return count
        count=renew()
        if count==0:
            # Same ordinary legacy grace/recheck for a just-created lease.
            time.sleep(0.5);count=renew()
        if count==0:raise LocalModelError(failure)
        response=tracked({'jsonrpc':'2.0','id':'preview-reservations','method':'resources/read',
            'params':{'uri':'resource://file_reservations/'+urllib.parse.quote(target['project_key'],safe='')
                +'?active_only=true&format=json'}})
        if reader(response,agent=target['name'],targets=paths,matcher_factory=lambda:matcher):
            raise LocalModelError(failure)
        checked()
        return {'ok':True,'protected':True,'renewed':count}
    except Exception:raise LocalModelError(failure) from None


def record_preview_session(intent,operation,payload):
    """Hook receipt only, under the same active proof and owner CAS as proxy.

    Payload never chooses an owner, PID, launch or credential. SessionEnd is
    not wait/reap/lease release/retire. A failed post-save proof leaves the
    receipt in place for status/explicit resolution, never rolls back facts.
    """
    failure='LOCAL_MODEL_SESSION_FAILED'
    try:
        event={'session-start':'SessionStart','end-session':'SessionEnd'}[operation]
        if (type(payload) is not dict or type(payload.get('session_id')) is not str
                or re.fullmatch('[A-Za-z0-9_-]{1,128}',payload['session_id']) is None
                or payload.get('cwd')!=intent['cwd']
                or ('hook_event_name' in payload and payload['hook_event_name']!=event)):
            raise LocalModelError(failure)
        auth,api,_,target,validate,binding_identity,initial,pinned=_active_context(intent)
        current=validate()
        if binding_identity(current)!=pinned:raise LocalModelError(failure)
        producer=api['runtime_legacy_execution'].ExecutionStore(target['runtime_dir'],target['name'])
        saved,digest=producer.store._snapshot()
        if saved['owner']!=current['owner']:raise LocalModelError(failure)
        row=deepcopy(current['row']);receipt=row['session_receipt'];session=payload['session_id']
        if receipt is not None and receipt['session_id']!=session:raise LocalModelError(failure)
        if operation=='session-start':
            if receipt is not None and receipt['phase']!='started':raise LocalModelError(failure)
            row['phase']='running';row['session_receipt']={'session_id':session,'phase':'started'}
        else:
            if row['phase']!='running' or receipt is None:raise LocalModelError(failure)
            row['session_receipt']={'session_id':session,'phase':'ended'}
        if row!=current['row']:producer._save(row,digest)
        after=validate()
        if binding_identity(after)!=pinned or after['row']!=row:raise LocalModelError(failure)
        return {'ok':True,'recorded':True}
    except Exception:raise LocalModelError(failure) from None
