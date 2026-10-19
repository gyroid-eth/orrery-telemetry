"""Explicit operator preparation/status over existing client truth, no UI journal."""
import json
import os
from pathlib import Path
import runpy
import hashlib

ROOT = Path(__file__).resolve().parents[2]
API = runpy.run_path(str(ROOT/'bin/lib/runtime_client.py'))
# Initialize the shared pure contracts before accepting concurrent operations.
API['operator_schemas']()
_UNSET = object()


class RuntimeOperator:
    def __init__(self, config_path, *, api=None):
        self.api=API if api is None else api
        self.path=self.api['absolute'](str(config_path))
        if self.path.name!='server-config.json':raise self.api['ClientError']('CONFIG_PATH_INVALID')
        self.raw=self.api['read_private'](self.path)
        self.config=self.api['read_json'](self.path)
        c=self.config
        if c.get('kind')!='orrery-global-server-s2c-v1' or c.get('activation_enabled') is not False:
            raise self.api['ClientError']('ISOLATED_CONFIG_REQUIRED')
        self.isolation=self.api['absolute'](c.get('isolation_root'))
        self.runtime=self.api['absolute'](c.get('runtime_root'))
        if self.path!=self.runtime/'server-config.json' or not self.runtime.is_relative_to(self.isolation):
            raise self.api['ClientError']('CONFIG_PATH_INVALID')
        self.api['safe_private_directory'](self.isolation)
        # Preparation owns the runtime's directory mode. Its private isolation
        # root and typed 0600 config are the admission boundary, as in the client.
        self.binding={'expected_server_instance_id':c.get('mail_instance_id'),
            'candidate_generation':c.get('candidate_generation'),'authority_epoch':c.get('authority_epoch')}
        if any(not isinstance(v,str) or not v for v in self.binding.values()):
            raise self.api['ClientError']('BINDING_INVALID')

    def read(self, query, *, deadline=None):
        # No local DB verification: the admitted server owns preparation proof,
        # tracker, authority fence and the single SQLite read transaction.
        if self.api['read_private'](self.path)!=self.raw:
            raise self.api['ClientError']('CONTEXT_CHANGED')
        reply=(self.api['operator_exchange'](self.config,self.binding,query) if deadline is None
               else self.api['operator_exchange'](self.config,self.binding,query,deadline=deadline))
        if self.api['read_private'](self.path)!=self.raw:
            raise self.api['ClientError']('CONTEXT_CHANGED')
        return reply


    @property
    def clients_parent(self):
        return self.isolation/'agentstack/clients'

    def path_for(self, name):
        self.api['validate_client_name'](name)
        return self.clients_parent/name/self.api['CLIENT_LAYOUT']['context']

    def client(self, name, *, read_only=False):
        path=self.path_for(name)
        pending=path.parent/self.api['CLIENT_LAYOUT']['registration']
        recovery=None
        if pending.exists() or pending.is_symlink():
            saved=self.api['read_json'](pending)
            if saved.get('row'):
                recovery=('finalize-create',saved['row']['agent_id'])
        client=self.api['RuntimeClient'](path,recovery=recovery,read_only=read_only)
        if client.binding!=self.binding or client.runtime!=self.runtime:
            raise self.api['ClientError']('CHILD_BINDING_MISMATCH')
        return client

    def abandon_registration(self, name, expected_digest):
        path=self.path_for(name)
        # An absent root is a missing target, not a private-file failure. Keep
        # all existing-root admission and pending digest checks in the client.
        try:path.parent.lstat()
        except FileNotFoundError:raise self.api['ClientError']('TARGET_UNAVAILABLE')
        return self.client(name).abandon_registration(expected_digest)

    def template(self):
        c=self.config
        # Verify the exact read-only inputs before creating any output. The
        # remote server validates preparation proof, never this caller's DB.
        self.read({'kind':'health'})
        lock=self.api['absolute'](c['authority_lock'])
        authority=self.api['absolute'](c['authority'])
        for path in (lock,authority):
            if not path.is_relative_to(self.isolation) or path.resolve()!=path:
                raise self.api['ClientError']('PATH_OUTSIDE_ISOLATION')
            self.api['read_private'](path)
        expected={'kind':'orrery-global-authority-v1','phase':'active','root_status':'active',
            'runtime_root':str(self.runtime),'mail_instance_id':self.binding['expected_server_instance_id'],
            'candidate_generation':self.binding['candidate_generation'],'authority_epoch':self.binding['authority_epoch']}
        value=self.api['read_json'](authority)
        if any(value.get(k)!=v for k,v in expected.items()):
            raise self.api['ClientError']('WRITER_FENCED')
        info=lock.lstat()
        return {'kind':self.api['KIND'],'mode':'global','activation_enabled':False,
            'wrapper_root':str(ROOT),'isolation_root':str(self.isolation),'runtime_root':str(self.runtime),
            'agentstack_home':str(self.isolation/'agentstack'),'authority':str(authority),
            'authority_lock':str(lock),'lock_identity':[info.st_dev,info.st_ino],
            'management_socket':c['management_socket'],
            'mcp_url':self.api['local_endpoint'](os.environ.get('AGENTSTACK_MCP_URL','')),**self.binding}

    def prepare(self, name):
        template=self.template()
        return self.api['prepare_client_root'](template,name)

    def provider(self, client):
        program=client.registration_metadata().get('program')
        mapping={'claude':'claude','claude-code':'claude','codex':'codex','gemini':'gemini','antigravity':'gemini','daemon':'daemon'}
        if program not in mapping:raise self.api['ClientError']('PROVIDER_UNAVAILABLE')
        return mapping[program]

    def configured(self, client):
        provider=self.provider(client)
        delivery=self.api['runtime_delivery'](client)
        if provider=='daemon':
            delivery.execution()
        elif provider in ('claude','codex'):
            home=client.runtime_dir/'provider-home'/provider
            helpers=self.api['delivery_contract']()['instruction_api']()
            try:
                helpers['provider_instructions'](home,provider,client.config['wrapper_root'],'')
            except helpers['Refused'] as exc:
                raise self.api['ClientError']('INSTRUCTION_PROVIDER_CONFIG_REQUIRED') from exc
            if provider=='codex':delivery.codex_trust()
        else:
            raise self.api['ClientError']('GEMINI_INTERACTIVE_RUNTIME_UNSUPPORTED')
        return provider

    def status(self, name):
        path=self.path_for(name)
        result={'ok':True,'client_name':name,'stage':'absent','agent_id':None,
            'next_operations':['prepare-root'],'reason':None,'launch_status':None,'registration_sha256':None}
        if not path.parent.exists() and not path.parent.is_symlink():return result
        try:
            client=self.client(name)
            with client.operation_mutex():
                pending=client.outputs['registration']
                if pending.exists() or pending.is_symlink():
                    raw=self.api['read_private'](pending)
                    result['registration_sha256']=hashlib.sha256(raw).hexdigest()
                    row=json.loads(raw)
                    if row.get('terminal'):
                        result.update(stage='unavailable',next_operations=['repair-local-state'],reason='REGISTRATION_OWNER_CONFLICT')
                    elif row.get('row'):
                        result.update(stage='registration_saved',agent_id=row['row']['agent_id'],next_operations=['finalize-registration'])
                    else:result.update(stage='registration_pending',next_operations=['replay-registration','registration-abandon'])
                elif client.identity is None:
                    result.update(stage='root_only',next_operations=['replay-registration'])
                else:
                    has_launch=client.outputs['launch'].exists() or client.outputs['launch'].is_symlink()
                    row=client.inspect_owner()
                    result['agent_id']=row['agent_id']
                    if has_launch:
                        delivery=self.api['runtime_delivery'](client)
                        saved=delivery.launch()
                        observed=delivery.status(saved['launch_id'])
                        result.update(stage='launch_present',next_operations=['launch-status'],
                            launch_status={k:observed[k] for k in ('launch_id','state_sha256','phase','operator_required','next_operations')})
                    else:
                        try:self.configured(client)
                        except (OSError,ValueError,RuntimeError) as exc:
                            result.update(stage='registered_unconfigured',next_operations=['configure-provider'],reason=str(exc))
                        else:result.update(stage='configured',next_operations=['launch'])
        except (OSError,ValueError,RuntimeError,KeyError,TypeError) as exc:
            result.update(stage='unavailable',next_operations=['repair-local-state'],reason=str(exc))
        self.api['wire_contract']()['validate_schema'](result,self.api['operator_schemas']()['FIXTURE']['spawn_status_schema'])
        return result

    def lookup(self, name=None):
        target=self.read({'kind':'resolve_name','name':name})['data']['agent_id'] if name is not None else None
        matches=[]
        diagnostics=[]
        if self.clients_parent.exists() or self.clients_parent.is_symlink():
            self.api['safe_private_directory'](self.clients_parent)
            for entry in sorted(self.clients_parent.iterdir()):
                try:
                    self.api['validate_client_name'](entry.name)
                    status=self.status(entry.name)
                    pending=entry/self.api['CLIENT_LAYOUT']['registration']
                    saved=self.api['read_json'](pending) if pending.exists() or pending.is_symlink() else {}
                    if status['stage']=='unavailable':
                        diagnostics.append({'client_name':entry.name,'reason':status['reason']})
                    if name is None or (target is not None and status['agent_id']==target) or saved.get('name')==name:
                        matches.append({k:status[k] for k in ('client_name','agent_id','stage')})
                except (OSError,ValueError,RuntimeError,KeyError,TypeError) as exc:
                    reason=str(exc)
                    if not reason.isascii() or not reason.replace('_','').isalnum() or not reason.isupper():
                        reason='CLIENT_LOOKUP_UNAVAILABLE'
                    diagnostics.append({'client_name':entry.name,'reason':reason})
        result={'ok':True,'matches':matches,'name_agent_id':target,'diagnostics':diagnostics}
        self.api['wire_contract']()['validate_schema'](result,self.api['operator_schemas']()['FIXTURE']['client_lookup_schema'])
        return result

    def create(self, name, *, agent_name, program, model):
        self.api['validate_client_name'](name)
        path=self.path_for(name)
        admission=None
        if isinstance(model,str) and model.startswith('local:'):
            if not path.exists() and not path.is_symlink():
                raise self.api['ClientError']('LOCAL_MODEL_INTENT_OWNER_REQUIRED')
            admission=self.client(name,read_only=True).check_local_registration(agent_name,program,model)
        elif path.exists() or path.is_symlink():
            # A configured local owner cannot be reinterpreted as a cloud
            # request merely because the registration caller changed a flag.
            admission=self.client(name,read_only=True).check_local_registration(agent_name,program,model)
        lookup=self.lookup(agent_name)
        known=lookup['matches']
        terminal={row['client_name'] for row in lookup['diagnostics'] if row['reason']=='REGISTRATION_OWNER_CONFLICT'}
        if (lookup['name_agent_id'] is not None and not known) or any(row['client_name'] in terminal for row in known):
            return {'ok':False,'reason':'NAME_CONFLICT','matches':[]}
        if any(row['client_name']!=name for row in known):
            reason='CLIENT_SELECTION_REQUIRED' if len(known)>1 else 'EXISTING_CLIENT_REQUIRED'
            return {'ok':False,'reason':reason,'matches':known}
        path=self.path_for(name)
        if not path.exists() and not path.is_symlink():self.prepare(name)
        client=self.client(name)
        with client.operation_mutex():
            if client.identity and not client.outputs['registration'].exists():
                if client.outputs['child'].exists():raise self.api['ClientError']('CLIENT_ROOT_IS_CHILD')
                metadata=client.registration_metadata()
                registered_model=admission['projection']['model'] if admission is not None else model
                if client.identity.get('name')!=agent_name or metadata.get('program')!=program or metadata.get('model')!=registered_model:
                    raise self.api['ClientError']('EXISTING_IDENTITY_REQUIRED')
                return self.status(name)
            client.create(agent_name,program,model)
        return self.status(name)

    def spawn(self, name, *, agent_name=_UNSET, program=_UNSET, model=_UNSET, cwd=_UNSET,
              expected_digest=None, caller=_UNSET, parent_client_name=None,
              prepare_only=False, task_delivery=_UNSET):
        if type(prepare_only) is not bool:
            raise self.api['ClientError']('LOCAL_MODEL_CALLER_INVALID')
        minimal = (agent_name,program,model,cwd)
        if caller is not _UNSET:
            if any(value is not _UNSET for value in minimal):
                raise self.api['ClientError']('LOCAL_MODEL_CALLER_INVALID')
            return self._spawn_caller(name,caller,parent_client_name=parent_client_name,
                prepare_only=prepare_only,task_delivery=task_delivery,expected_digest=expected_digest)
        if (any(value is _UNSET for value in minimal) or parent_client_name is not None
                or task_delivery is not _UNSET):
            raise self.api['ClientError']('LOCAL_MODEL_CALLER_INVALID')
        if expected_digest is not None:
            client=self.client(name)
            with client.operation_mutex():
                with client.fence():
                    pending=client.outputs['registration']
                    if not pending.exists() and not pending.is_symlink():raise self.api['ClientError']('TARGET_UNAVAILABLE')
                    if hashlib.sha256(self.api['read_private'](pending)).hexdigest()!=expected_digest:
                        raise self.api['ClientError']('REGISTRATION_PENDING_CONFLICT')
                return self.spawn(name,agent_name=agent_name,program=program,model=model,cwd=cwd,
                                  prepare_only=prepare_only)
        result=self.create(name,agent_name=agent_name,program=program,model=model)
        if not result['ok'] or result['stage']=='launch_present':return result
        client=self.client(name)
        delivery=self.api['runtime_delivery'](client)
        provider=self.provider(client)
        # This POST is explicit configuration; regular status/resume never
        # rewrites provider instructions or grants Codex trust.
        if provider in ('claude','codex'):delivery.instruction_configure(provider)
        try:self.configured(client)
        except (OSError,ValueError,RuntimeError):return self.status(name)
        if not prepare_only:delivery.start(provider,cwd,detached=True)
        return self.status(name)

    def _spawn_caller(self, name, caller, *, parent_client_name, prepare_only,
                      task_delivery, expected_digest):
        from runtime_local_launch import validate_caller_request, admit_caller_request
        caller=validate_caller_request(caller)
        if not isinstance(parent_client_name,str):
            raise self.api['ClientError']('CLIENT_NAME_INVALID')
        self.api['validate_client_name'](parent_client_name)
        if parent_client_name==name:raise self.api['ClientError']('CLIENT_ROOT_OVERLAP')
        delivery_kind=caller.get('task_delivery','mail')
        if task_delivery is not _UNSET and task_delivery!=delivery_kind:
            raise self.api['ClientError']('LOCAL_MODEL_INTENT_CHANGED')
        path=self.path_for(name)
        if not path.exists() and not path.is_symlink():
            raise self.api['ClientError']('LOCAL_MODEL_INTENT_OWNER_REQUIRED')
        target=self.client(name,read_only=True)
        # Initial admission is non-creating. Root preparation takes C itself,
        # so never hold an outer C while entering prepare_child. Its existing
        # child C repeats this exact admission before registration writes.
        checked=admit_caller_request(target,caller)
        digest=checked['intent']['intent_digest']
        parent=self.client(parent_client_name,read_only=True)
        if not parent.identity or parent.identity.get('name')!=caller['parent']:
            raise self.api['ClientError']('CHILD_PARENT_MISMATCH')
        program={'codex':'codex','claude':'claude-code'}[caller['provider']]
        options={**caller,'child_client_name':name,'program':program,
            'workdir':caller['cwd'],'task_file':caller['task'],
            'parsed_tools':caller['tools'],'prepare_only':True,
            'embed_task':delivery_kind=='embedded'}
        prepared=parent.prepare_child(options,_caller=caller,_intent_digest=digest,
                                       _registration_sha256=expected_digest)
        if prepared['client_config']!=str(path):
            raise self.api['ClientError']('CHILD_BINDING_MISMATCH')
        # Registration activated this context; never reuse its old raw or
        # identity snapshot, and recheck the same full intent before task IO.
        target=self.client(name)
        with target.operation_mutex():
            admit_caller_request(target,caller,expected_digest=digest)
            target._prepare_child_launch(caller['task'],embed_task=delivery_kind=='embedded')
        if not prepare_only:
            delivery=self.api['runtime_delivery'](target)
            self.configured(target)
            delivery.start(caller['provider'],caller['cwd'],detached=caller['detached'],
                           expected_intent_digest=digest)
        return self.status(name)

    def resume(self, name, *, prepare_only=False):
        client=self.client(name)
        if client.identity:
            metadata=client.registration_metadata()
            client.check_local_registration(client.identity['name'],metadata.get('program'),metadata.get('model'),metadata=True)
        current=self.status(name)
        if current['stage']!='launch_present':
            return current
        saved=current['launch_status']
        if saved['operator_required']:raise self.api['ClientError']('LAUNCH_OUTCOME_UNKNOWN')
        if saved['phase'] not in ('ended','cancelled'):return current
        delivery=self.api['runtime_delivery'](client)
        provider=self.configured(client)
        receipt=self.api['read_json'](client.runtime_dir/'session_index/self.json')
        if not receipt.get('session_id') or receipt.get('agent_id')!=client.identity['agent_id']:
            raise self.api['ClientError']('PROVIDER_SESSION_REQUIRED')
        previous=delivery.launch()
        if client.outputs['child'].exists():client.resume_child({})
        else:
            if client.session_liveness()!='absent':raise self.api['ClientError']('SESSION_LIVENESS_UNKNOWN')
            observed=client.inspect_owner()
            if observed.get('retired_at') is not None:client.call('unretire_agent')
            client.reconnect()
        if not prepare_only:
            delivery.start(provider,previous['cwd'],detached=True,launch_kind='resume',provider_session_id=receipt['session_id'])
        return self.status(name)
