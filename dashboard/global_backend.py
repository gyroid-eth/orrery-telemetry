"""Dashboard's private operator bridge. Never opens a Mail database."""
import json
import os
from pathlib import Path
import runpy
import re
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
# Python's module import lock initializes this one operator/client API before
# concurrent HTTP requests. No per-request temporary module aliases.
OPERATOR_API=runpy.run_path(str(ROOT/'bin/lib/runtime_operator.py'))
OBSERVATION_API=runpy.run_path(str(ROOT/'bin/lib/runtime_observation.py'))


class BackendError(RuntimeError):
    def __init__(self, reason, status=503):
        super().__init__(reason)
        self.status=status


class Backend:
    def __init__(self, config_path):
        self.observation_deadline=time.monotonic()+5
        self.operator=OPERATOR_API['RuntimeOperator'](config_path)

    def read(self, query):
        return {'runtime_mode':'global',**self.operator.read(query)}

    def instruction(self, agent_id, session_id):
        helper=OPERATOR_API['API']['delivery_contract']()['instruction_api']()
        unknown={'provider':None,'target_path':None,'findings':[],
            'checked_at':datetime.now(timezone.utc).isoformat(),'disk_state':'unknown',
            'session_state':'unknown','evidence_kind':'candidate-observation-not-effective-load',
            'reason':'PROVIDER_HOME_UNPROVEN'}
        # Enumerate fixed client slots via the existing operator, never caller paths.
        slots=[]
        if self.operator.clients_parent.exists() or self.operator.clients_parent.is_symlink():
            OPERATOR_API['API']['safe_private_directory'](self.operator.clients_parent)
            for entry in sorted(self.operator.clients_parent.iterdir()):
                try:
                    path=self.operator.path_for(entry.name)
                    OPERATOR_API['API']['safe_private_directory'](entry)
                    local=OPERATOR_API['API']['read_json'](path)
                    if (local.get('identity') or {}).get('agent_id')==agent_id:
                        slots.append({'client_name':entry.name})
                except (RuntimeError,OSError,ValueError,TypeError,KeyError):
                    continue
        candidates=[]
        for slot in slots:
            try:
                c=self.operator.client(slot['client_name'])
                with c.operation_mutex(),c.fence():
                    if not c.identity or c.identity['agent_id']!=agent_id:continue
                    c.inspect_owner()
                    session=c.runtime_dir/'session_index/self.json'
                    if not session.exists() and not session.is_symlink():continue
                    saved=OPERATOR_API['API']['read_json'](session)
                    if session_id and saved.get('session_id')!=session_id:continue
                    launch=OPERATOR_API['API']['read_json'](c.outputs['launch'])
                    provider=launch.get('provider')
                    if provider not in ('claude','codex'):continue
                    home=c.runtime_dir/'provider-home'/provider
                    OPERATOR_API['API']['safe_private_directory'](home)
                    candidates.append(helper['instruction_status'](provider,home,c.config['wrapper_root'],saved['cwd'],saved.get('instruction_observation')))
            except (RuntimeError,OSError,ValueError,TypeError,KeyError,helper['Refused']):
                candidates.append({**unknown,'reason':'INSTRUCTION_OBSERVATION_UNAVAILABLE'})
        result=candidates[0] if len(candidates)==1 else {**unknown,'reason':'AMBIGUOUS_INSTRUCTION_HOME' if candidates else unknown['reason']}
        helper['validate'](result,'status')
        return {'ok':True,'runtime_mode':'global','agent_id':agent_id,'session_id':session_id,'data':result}

    def observe(self, ids, parser):
        return OBSERVATION_API['Observations'](self.operator, parser, deadline=self.observation_deadline).read(ids)

    def get(self, path, params):
        def one(key,default=None):
            values=params.get(key,[])
            if len(values)>1:raise BackendError('QUERY_INVALID',400)
            return values[0] if values else default
        def number(key,default=None):
            value=one(key,default)
            if value is None:return None
            if not str(value).isascii() or not str(value).isdigit() or not 1<=int(value)<=2**63-1:
                raise BackendError('QUERY_INVALID',400)
            return int(value)
        if path=='/api/instruction-migration':
            if set(params)-{'agent_id','session_id'}:raise BackendError('QUERY_INVALID',400)
            agent_id=number('agent_id');session_id=one('session_id')
            if agent_id is None or session_id is not None and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',session_id) is None:
                raise BackendError('QUERY_INVALID',400)
            return self.instruction(agent_id,session_id)
        if path=='/api/global/clients':return self.operator.lookup(one('name'))
        if path=='/api/global/spawn-status':return self.operator.status(one('client_name'))
        if path=='/api/agents':
            return self.read(dict(kind='roster',before_agent_id=number('before_agent_id'),limit=number('limit',200)))
        if path=='/api/mail-watcher-health':return self.read(dict(kind='health'))
        supported={'/api/mail/recent','/api/mail/message','/api/mail/conversation',
            '/api/mail/legacy-label','/api/messages-since','/api/agent-history','/api/edge-messages','/api/graph'}
        if path not in supported:raise BackendError('GLOBAL_ROUTE_UNAVAILABLE',404)
        aid=number('view_as_agent_id')
        if aid is None:raise BackendError('UI_VIEWPOINT_REQUIRED',400)
        limit=number('limit',100);cursor=one('cursor')
        if path=='/api/mail/message':q=dict(kind='message',message_id=number('id'),view_as_agent_id=aid)
        elif path in {'/api/mail/conversation','/api/mail/legacy-label'}:
            if 'source' in params:raise BackendError('QUERY_INVALID',400)
            args={'cursor':cursor,'per_thread_limit':limit}
            if path.endswith('conversation'):args['message_id']=number('message_id')
            else:args['thread_id']=one('label')
            q=dict(kind='messages',tool='summarize_thread',view_as_agent_id=aid,arguments=args)
        elif path=='/api/edge-messages':
            q=dict(kind='edge_messages',view_as_agent_id=aid,a_agent_id=number('a_agent_id'),
                b_agent_id=number('b_agent_id'),limit=limit,cursor=cursor)
        else:
            args={'query':one('query',''),'limit':limit,'cursor':cursor,'include_bodies':False}
            if path=='/api/mail/recent':
                q=dict(kind='messages',tool='search_messages',view_as_agent_id=aid,arguments=args)
            else:
                q=dict(kind='activity',view_as_agent_id=aid,limit=limit,cursor=cursor)
                if path=='/api/agent-history':q['target_agent_id']=number('agent_id')
                if path=='/api/messages-since':
                    value=one('after_id','0')
                    if not value.isascii() or not value.isdigit():raise BackendError('QUERY_INVALID',400)
                    q['after_id']=int(value)
        return self.read(q)

    def spawn(self,payload,validate_cwd):
        """One literal HTTP adapter to the existing minimal/full operator API."""
        api=self.operator.api
        schema=api['operator_schemas']()['FIXTURE']['http_spawn_schema']
        try:api['wire_contract']()['validate_schema'](payload,schema)
        except ValueError as exc:raise BackendError('QUERY_INVALID',400) from exc
        if 'caller' in payload:
            from runtime_local_launch import validate_caller_request
            # Structural/tool validation is shared and effect-free. Never
            # recover omitted fields from the configured owner or HTTP aliases.
            try:caller=validate_caller_request(payload['caller'])
            except ValueError as exc:raise BackendError('CHILD_TOOLS_INVALID',400) from exc
            # R2 foreground transfers control to the calling CLI (SystemExit).
            # An HTTP handler cannot own that runner or silently change intent.
            if not payload['prepare_only'] and not caller['detached']:
                raise BackendError('HTTP_FOREGROUND_UNSUPPORTED',400)
            # The directory guard resolves for containment only. The admitted
            # caller literal, not that canonicalization, must match the intent.
            validate_cwd(caller['cwd'])
            options={'caller':caller,'parent_client_name':payload['parent_client_name'],
                'prepare_only':payload['prepare_only'],'expected_digest':payload.get('expected_digest')}
            if 'task_delivery' in payload:options['task_delivery']=payload['task_delivery']
            return self.operator.spawn(payload['client_name'],**options)
        cwd=validate_cwd(payload['cwd'])
        return self.operator.spawn(payload['client_name'],agent_name=payload['name'],
            program=payload['program'],model=payload['model'],cwd=cwd,expected_digest=payload.get('expected_digest'))

    def post(self,path,payload,validate_cwd,open_terminal):
        if path=='/api/global/spawn':return self.spawn(payload,validate_cwd)
        table={'/api/global/resume':'http_resume_schema',
            '/api/global/registration-abandon':'http_registration_abandon_schema'}
        if path not in table:raise BackendError('GLOBAL_ROUTE_UNAVAILABLE',404)
        api=self.operator.api
        schema=api['operator_schemas']()['FIXTURE'][table[path]]
        try:api['wire_contract']()['validate_schema'](payload,schema)
        except ValueError as exc:raise BackendError('QUERY_INVALID',400) from exc
        if path.endswith('/registration-abandon'):
            return self.operator.abandon_registration(payload['client_name'],payload['expected_digest'])
        result=self.operator.resume(payload['client_name'],prepare_only=payload.get('prepare_only',False))
        launch=result.get('launch_status')
        if (payload.get('open',True) and not payload.get('prepare_only',False)
                and launch and launch['phase'] in ('gated','running') and not launch['operator_required']):
            target_api=api['runtime_tmux_api']()
            try:
                targets=target_api['module_api']('runtime_target')['Targets'](self.operator,api=api,transport_api=target_api)
                aid=result.get('agent_id')
                if aid is None:
                    context=api['read_json'](self.operator.path_for(payload['client_name']))
                    aid=(context.get('identity') or {}).get('agent_id')
                attached,_=targets.execute('attach',aid,operator=True,print_only=True)
                # Re-admit immediately before the existing terminal adapter.
                checked,_=targets.execute('attach',aid,operator=True,print_only=True)
                if attached['state_sha256']!=checked['state_sha256']:raise BackendError('TMUX_TARGET_CHANGED')
                # The terminal executes the common CLI, which checks again after
                # the adapter/WT handoff. Printed tmux argv is diagnostic only.
                entry=Path(__file__).resolve().parents[1]/'bin/lib/runtime_client.py'
                opened=open_terminal([__import__('sys').executable,str(entry),'attach','--operator',
                    '--runtime-mode','global','--server-config',str(self.operator.path),
                    '--agent-id',str(aid)],title=payload['client_name'])
                if not opened.get('ok'):raise BackendError('TERMINAL_OPEN_FAILED')
            except target_api['TargetError'] as exc:
                raise BackendError('TERMINAL_OPEN_FAILED') from exc
        return result
