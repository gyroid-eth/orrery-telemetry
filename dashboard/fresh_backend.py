"""Explicit fresh dashboard adapter. Browser JSON never selects an authority.

The protected dashboard client is a preparation root, not an implicit task
parent. Only bounded public projections leave this module. Legacy Mail,
credentials, process discovery and default tmux sockets are never consulted.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import runpy
import time
import threading
from uuid import UUID, uuid4

from agentstack_mail.fresh_child import prepare, handoff, status, family
from agentstack_mail.fresh_client import FreshBoundClient
from agentstack_mail.fresh_launch import identity, pinned
from agentstack_mail.fresh_runtime import FreshRuntime, _directory, _read_private, _write_private
from agentstack_mail.storage_origin_contract import B1Error, name_keys

CAPABILITIES = {'spawn': True, 'roster': True, 'mail': True, 'graph': True, 'terminal': True,
                'worktree': False, 'browser': False, 'tools': False}
CLEANUP_ATTACHED_MESSAGE='Close the attached Terminal/Cockpit views and wait for their observers to stop before checking again.'


class Backend:
    def __init__(self, context, root, *, transport=None):
        self.client = FreshBoundClient(context)
        self.root = Path(root).resolve(strict=True)
        # This callable is trusted host/test injection, never an HTTP field.
        self.transport = transport
        self._display_lock=threading.Lock()
        self._display_cache=None
        self._display_offset=0
        self.jobs = self.client.path.parent / 'dashboard-jobs'
        try:
            from dashboard.fresh_readers import Reader
        except ModuleNotFoundError as exc:
            if exc.name != 'dashboard':raise
            from fresh_readers import Reader
        self.reader = Reader()

    def host_targets(self, runtime):
        from agentstack_mail.fresh_targets import Targets
        return Targets(self.client, self.root, runtime=runtime)

    async def target_read(self, path, query, *, read_events=None):
        from agentstack_mail.fresh_targets import Targets, SELECTORS
        allowed=SELECTORS|({'lines'} if path=='/api/term' else {'limit'})
        if set(query)-allowed:raise B1Error('FRESH_TARGET_QUERY_INVALID')
        targets=Targets(self.client,self.root)
        selected={k:v for k,v in query.items() if k in SELECTORS}
        if path=='/api/term':return await targets.capture(selected,query.get('lines',500))
        return await targets.history(selected,query.get('limit',100),read_events)

    async def target_action(self, path, payload, *, opener=None, open_default=True):
        from agentstack_mail.fresh_targets import Targets, SELECTORS
        extra={'mode'} if path=='/api/kill' else {'open'}
        if set(payload)-SELECTORS-extra or 'open' in payload and type(payload['open']) is not bool:
            raise B1Error('FRESH_TARGET_QUERY_INVALID')
        targets=Targets(self.client,self.root)
        query={k:v for k,v in payload.items() if k in SELECTORS}
        if path=='/api/kill':
            result=await targets.cleanup(query,mode=payload.get('mode','both'))
            if result.get('error')=='FRESH_CLEANUP_ATTACHED':
                result.update(error=CLEANUP_ATTACHED_MESSAGE,error_code='FRESH_CLEANUP_ATTACHED')
            return result
        if path=='/api/exit':
            if 'open' in payload:raise B1Error('FRESH_TARGET_QUERY_INVALID')
            return await targets.exit(query)
        return await targets.jump(query,open_terminal=payload.get('open',open_default),opener=opener)

    async def read(self, route, query):
        context, proof = self.client._load()
        runtime = await FreshRuntime.open(Path(context['root']) / 'config.json')
        try:
            async with runtime.core.owner_transaction(context['mail_instance_id'], proof) as owner:
                result = await self.reader.read(owner, context['mail_instance_id'], route, query)
            if route == '/api/graph':
                selected=result.pop('_selected')
                # Only saved, verified logical families; never infer parenthood
                # from a preparation root, matching name, or latest job.
                ids={a['mail_agent_id']:a for a in (await self.roster())['agents']}
                for aid, clients in self._contexts().items():
                    if len(clients)!=1 or aid not in ids or aid not in selected:continue
                    try:
                        parent, _, ready=family(clients[0])
                        pid=parent.context['mail_agent_id']
                        if not ready or ready.get('standalone') or pid not in ids or pid not in selected:continue
                        result['spawn'].append(dict(source=ids[pid]['name'],target=ids[aid]['name'],
                            mail_instance_id=context['mail_instance_id'],source_agent_id=pid,target_agent_id=aid))
                    except (B1Error,OSError,ValueError,KeyError):continue
            return result
        finally:await runtime.close()

    async def roster(self):
        from agentstack_mail.fresh_targets import Targets, Selected
        context, proof = self.client._load()
        runtime = await FreshRuntime.open(Path(context['root']) / 'config.json')
        try:
            async with runtime.core.owner_transaction(context['mail_instance_id'], proof) as owner:
                if len(owner.records) > 5000:raise B1Error('FRESH_ROSTER_LIMIT')
                generations={a.id:a.credential_generation for a in owner.records.values()}
                revision=owner.state['revision']
                rows = [dict(mail_instance_id=context['mail_instance_id'], mail_agent_id=a.id,
                    name=a.name, program=a.program, model=a.model, provider=a.program,
                    retired=a.retired_at is not None, running=None,
                    state='retired' if a.retired_at is not None else 'unknown',
                    last_active=a.last_active_ts.timestamp()) for a in sorted(owner.records.values(), key=lambda a:a.id)]
            contexts=self._contexts();selections=[]
            for row in rows:
                try:
                    client=self.context_for(row,contexts=contexts)
                    if client.context['credential_generation']!=generations[row['mail_agent_id']]:continue
                    _,_,ready=family(client)
                    if ready and ready.get('managed'):
                        backend=ready['managed']['selection']['backend_id']
                        row['provider']=backend
                        if backend=='strata':row['locality']='local'
                    launch=client.path.parent/'launch';current=launch/'current.json'
                    if not current.exists():continue
                    launch_id=_read_private(current)['launch_id']
                    if str(UUID(launch_id))!=launch_id:raise B1Error('FRESH_LAUNCH_RECEIPT_MISMATCH')
                    run=launch/'runs'/launch_id
                    intent=_read_private(run/'intent.json');pinned(client,intent)
                    ended=(run/'ended.json').exists()
                    row.update(running=False if ended else None,state='retired' if row['retired'] else 'finished' if ended else 'unknown')
                    if (run/'session.json').exists():
                        receipt=_read_private(run/'session.json');pinned(client,receipt)
                        if receipt.get('launch_id')!=launch_id or receipt.get('provider')!=intent['provider']:
                            raise B1Error('FRESH_LAUNCH_RECEIPT_MISMATCH')
                        row.update(native_session_id=receipt['native_session_id'],native_ready=True)
                        if len(selections)<128 and (run/'tmux.json').exists():
                            route=_read_private(run/'tmux.json');pinned(client,route)
                            Targets.validate_display_route(route)
                            outcome=_read_private(run/'ended.json') if ended else None
                            if outcome is not None and (set(outcome)!={'launch_id','exit_code'} or outcome['launch_id']!=launch_id
                                    or type(outcome['exit_code']) is not int):raise B1Error('FRESH_LAUNCH_OUTCOME_UNKNOWN')
                            if route.get('launch_id')!=launch_id:raise B1Error('FRESH_TMUX_TARGET_CHANGED')
                            from agentstack_mail.storage_origin_contract import digest
                            signature=digest(dict(context=client.context_digest,intent=intent,receipt=receipt,route=route,ended=outcome))
                            selections.append(Selected(client,row['name'],run,intent,receipt,outcome,route,signature))
                    # A receipt proves native startup, not a currently live OS process.
                except (B1Error,OSError,ValueError,KeyError):
                    continue
            key=(self.client.context_digest,revision,tuple((s.client.context['mail_agent_id'],s.signature) for s in selections))
            now=time.monotonic();cache=self._display_cache
            if not(cache is not None and cache[0]==key and now-cache[1]<5) and self._display_lock.acquire(blocking=False):
                try:
                    # Recheck after acquiring: another GET may have just published.
                    cache=self._display_cache
                    if not(cache is not None and cache[0]==key and now-cache[1]<5):
                        groups={}
                        for selected in selections:
                            socket=(selected.route['socket_path'],tuple(selected.route['socket_identity']))
                            groups.setdefault(socket,[]).append(selected)
                        batches=list(groups.values())
                        offset=self._display_offset%len(batches) if batches else 0
                        rotated=[s for group in batches[offset:]+batches[:offset] for s in group]
                        facts=Targets(self.client,self.root).display_snapshot(rotated,deadline=now+.25) if rotated else {}
                        self._display_offset=offset+2
                        self._display_cache=(key,now,facts)
                finally:self._display_lock.release()
            cache=self._display_cache
            valid=cache is not None and cache[0]==key and time.monotonic()-cache[1]<5
            for row in rows:
                fact=cache[2].get(row['mail_agent_id']) if valid else None
                row.update(category='unknown',running=None,attached=True,
                    observation=dict(state='unknown',age_ms=None,reason='unobserved'))
                if fact is not None:
                    row.update(fact,observation=dict(state='observed',age_ms=round((time.monotonic()-cache[1])*1000),reason=None))
            return dict(mail_instance_id=context['mail_instance_id'], agents=rows,observation_truncated=len(contexts)>128)
        finally:await runtime.close()

    async def resolve(self, *, name=None, ref=None):
        roster = await self.roster()
        if ref is not None:
            if (not isinstance(ref, dict) or set(ref) != {'mail_instance_id','mail_agent_id'}
                    or ref['mail_instance_id'] != roster['mail_instance_id'] or type(ref['mail_agent_id']) is not int):
                raise B1Error('FRESH_TARGET_INSTANCE_MISMATCH')
            candidates = [a for a in roster['agents'] if a['mail_agent_id'] == ref['mail_agent_id']]
            if name and any(a['name'] != name for a in candidates):raise B1Error('TARGET_NAME_STALE')
        else:
            keys = set(name_keys(name))
            candidates = [a for a in roster['agents'] if keys.intersection(name_keys(a['name']))]
        if len(candidates) != 1:
            raise B1Error('TARGET_NAME_AMBIGUOUS' if candidates else 'TARGET_NAME_NOT_FOUND',
                candidates=[{k:a[k] for k in ('mail_instance_id','mail_agent_id','name','retired')} for a in candidates])
        if candidates[0]['retired']:raise B1Error('TARGET_RETIRED')
        return candidates[0]

    def _contexts(self):
        root = Path(self.client.context['root']) / 'clients';_directory(root)
        paths = list(root.iterdir())
        if len(paths) > 5000:raise B1Error('FRESH_ROSTER_LIMIT')
        contexts = {}
        for folder in paths:
            # Only canonical client UUID directories from this protected root.
            try:
                if str(UUID(folder.name)) != folder.name:continue
                _directory(folder)
                client = FreshBoundClient(folder / 'context.json')
            except (B1Error, OSError, ValueError):continue
            if (client.context['root'] == self.client.context['root'] and
                    client.context['mail_instance_id']==self.client.context['mail_instance_id']):
                contexts.setdefault(client.context['mail_agent_id'],[]).append(client)
        return contexts

    def context_for(self, row, *, contexts=None):
        if row['mail_instance_id']!=self.client.context['mail_instance_id']:raise B1Error('INSTANCE_MISMATCH')
        matches=(self._contexts() if contexts is None else contexts).get(row['mail_agent_id'],[])
        if len(matches) != 1:raise B1Error('FRESH_PARENT_CONTEXT_UNAVAILABLE')
        return matches[0]

    @staticmethod
    def validate(payload, roots):
        allowed = {'parent','parent_ref','standalone','task','name','dir','provider','model','harness',
                   'url_index','url_key','effort','helper_model','async','role','group','emoji','resources',
                   'headless','worktree','worktree_base','claude_chrome','claude_chrome_device','base','tools','dry_run'}
        if not isinstance(payload, dict) or set(payload) - allowed:raise B1Error('FRESH_SPAWN_FIELDS_INVALID')
        for key in ('standalone','async','headless','worktree','claude_chrome','dry_run'):
            if key in payload and type(payload[key]) is not bool:raise B1Error('FRESH_SPAWN_ARGUMENTS_INVALID')
        for key in ('task','name','dir','provider','model','parent','role','group','emoji','effort','harness','helper_model'):
            if key in payload and not isinstance(payload[key],str):raise B1Error('FRESH_SPAWN_ARGUMENTS_INVALID')
        if (payload.get('worktree') or payload.get('worktree_base') or payload.get('claude_chrome')
                or payload.get('claude_chrome_device') or payload.get('base') not in (None,'default')
                or payload.get('tools') or payload.get('headless') is False or payload.get('dry_run')):
            raise B1Error('FRESH_SPAWN_FEATURE_UNAVAILABLE')
        if any(payload.get(k) for k in ('role','group','emoji')):
            raise B1Error('FRESH_SPAWN_ANNOTATION_UNAVAILABLE')
        if payload.get('standalone') and (payload.get('parent') or payload.get('parent_ref')):
            raise B1Error('FRESH_LOGICAL_PARENT_CONFLICT')
        if not payload.get('standalone') and not (payload.get('parent') or payload.get('parent_ref')):
            raise B1Error('FRESH_LOGICAL_PARENT_REQUIRED')
        task=payload.get('task','')
        if not task.strip() or len(task.encode())>65536:raise B1Error('FRESH_CHILD_TASK_INVALID')
        name=payload.get('name','')
        if name and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}',name):raise B1Error('FRESH_SPAWN_NAME_INVALID')
        raw=payload.get('dir','')
        if not raw:raise B1Error('FRESH_SPAWN_DIRECTORY_REQUIRED')
        cwd=Path(raw).expanduser().resolve(strict=True)
        if not cwd.is_dir() or not any(cwd==Path(r) or Path(r) in cwd.parents for r in roots):
            raise B1Error('FRESH_SPAWN_DIRECTORY_OUTSIDE_ROOTS')
        resources=payload.get('resources',[])
        if isinstance(resources,str):resources=[p.strip() for p in resources.split(',') if p.strip()]
        if not isinstance(resources,list) or any(not isinstance(p,str) or not p for p in resources):
            raise B1Error('FRESH_CHILD_RESOURCES_REQUIRED')
        return dict(cwd=str(cwd),resources=resources)

    async def spawn(self, payload, spec, roots):
        request=self.validate(payload,roots)
        # Resolve first, including retired same-name candidates. Never replace
        # an explicitly requested parent with the preparation root.
        parent=self.client if payload.get('standalone') else self.context_for(await self.resolve(
            name=payload.get('parent'),ref=payload.get('parent_ref')))
        name=payload.get('name')
        if not name:raise B1Error('FRESH_SPAWN_NAME_REQUIRED')
        occupied=await self.roster()
        if any(set(name_keys(name)).intersection(name_keys(a['name'])) for a in occupied['agents']):
            raise B1Error('FRESH_SPAWN_NAME_OCCUPIED')
        managed=None
        if spec.program in {'dsh','hermes'}:
            from agentstack_mail.fresh_harness import selection
            selected=dict(manifest_file=str(Path(os.environ['AGENTSTACK_MANAGED_SPAWN_CONFIG']).resolve(strict=True)),
                          backend_id=spec.provider)
            for key in ('effort','helper_model','url_index','url_key'):
                if key in payload:selected[key]=payload[key]
            if spec.effort:selected['effort']=spec.effort
            _,_,pins=selection(self.root,spec.program,spec.model,selected)
            managed=dict(selection=selected,pins=pins)
        job=str(uuid4());slot='dashboard_'+job.replace('-','')
        ready=await prepare(parent,slot,name,'codex' if spec.provider=='codex' else spec.program,spec.model,
            payload['task'][:2048],managed=managed,standalone=payload.get('standalone',False),
            effort=spec.effort or None if managed is None else None)
        child=FreshBoundClient(ready['context_path'])
        self.jobs.mkdir(mode=0o700,exist_ok=True);_directory(self.jobs)
        record=dict(job_id=job,slot=slot,name=name,parent_context=str(parent.path),
            parent_digest=parent.context_digest,child_context=str(child.path),child_digest=child.context_digest,
            logical_parent=None if payload.get('standalone') else identity(parent),created=time.time(),**identity(child))
        _write_private(self.jobs/(job+'.json'),record)
        try:
            transport=self.transport() if self.transport else self._transport()
            await handoff(parent,self.root,slot=slot,cwd=request['cwd'],task=payload['task'],resources=request['resources'],
                          unsafe_no_resources=not request['resources'],transport=transport)
        except (B1Error,OSError,ValueError):
            # Preparation may have committed: retain this exact job/identity.
            # Status reads do not retry an unknown external launch.
            raise B1Error('FRESH_SPAWN_PREPARED_NOT_CONFIRMED',job_id=job,
                          mail_instance_id=record['mail_instance_id'],mail_agent_id=record['mail_agent_id']) from None
        return dict(ok=True,pending=True,child_name=name,job_id=job,
                    mail_instance_id=record['mail_instance_id'],mail_agent_id=record['mail_agent_id'])

    def _transport(self):
        path=os.environ.get('AGENTSTACK_FRESH_DASHBOARD_SOCKET')
        if not path:raise B1Error('FRESH_DASHBOARD_SOCKET_REQUIRED')
        return runpy.run_path(str(self.root/'bin/lib/runtime_tmux.py'))['Transport'](path)

    async def spawn_status(self, name=None, ref=None):
        row=await self.resolve(name=name,ref=ref)
        if not self.jobs.exists():raise B1Error('FRESH_SPAWN_JOB_NOT_FOUND')
        _directory(self.jobs)
        paths=list(self.jobs.iterdir())
        if len(paths)>5000:raise B1Error('FRESH_SPAWN_JOB_LIMIT')
        records=[_read_private(p) for p in paths if p.suffix=='.json']
        records=[r for r in records if r['mail_instance_id']==row['mail_instance_id'] and r['mail_agent_id']==row['mail_agent_id']]
        if len(records)!=1:raise B1Error('FRESH_SPAWN_JOB_NOT_FOUND')
        saved=records[0];child=FreshBoundClient(saved['child_context']);parent,base,_=family(child)
        if (child.context_digest!=saved['child_digest'] or parent.context_digest!=saved['parent_digest']
                or str(parent.path)!=saved['parent_context']):raise B1Error('FRESH_CHILD_BINDING_MISMATCH')
        await child.call('reconnect',{})
        result=status(child,base) if (base/'handoff.json').exists() else dict(accepted=True,execution='not_started',native_ready=False)
        result={k:v for k,v in result.items() if k in {'accepted','execution','native_ready','mail_instance_id','mail_agent_id'}}
        # This is the existing caller's launch verdict, not process liveness.
        # Ending before a native receipt must never turn an attempt into success.
        state='ready' if result['native_ready'] else 'failed' if result['execution']=='ended' else 'launching'
        return dict(ok=True,name=row['name'],state=state,age=max(0,time.time()-saved['created']),
            error='FRESH_NATIVE_STARTUP_UNCONFIRMED' if state=='failed' else None,
            detail=result['execution'],result=result if state!='launching' else None,
            job_id=saved['job_id'],mail_instance_id=row['mail_instance_id'],mail_agent_id=row['mail_agent_id'])


def failure(exc):
    # No exception text, argv, context path or owner proof on the public wire.
    if isinstance(exc,B1Error):
        details={k:v for k,v in exc.details.items() if k in {'candidates','job_id','mail_instance_id','mail_agent_id'}}
        return dict(ok=False,error=CLEANUP_ATTACHED_MESSAGE if exc.code=='FRESH_CLEANUP_ATTACHED' else exc.code,
                    error_code=exc.code,**details)
    return dict(ok=False,error='FRESH_DASHBOARD_UNAVAILABLE',error_code='FRESH_DASHBOARD_UNAVAILABLE')
