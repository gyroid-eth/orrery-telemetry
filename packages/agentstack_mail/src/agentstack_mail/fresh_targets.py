"""Trusted-host current targets for fresh PC/bridge consumers.

Not a model tool or a second identity registry. The operator and target are
verified against the same B1 owner transaction. OS observations are rechecked
at each operation; they do not prove that a native provider understood input.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
from uuid import uuid4

from . import fresh_delivery as delivery
from .fresh_client import FreshBoundClient
from .fresh_launch import identity, native_id, operation, pinned, root_path
from .fresh_runtime import FreshRuntime, _directory, _read_private, _write_private
from .storage_origin_contract import B1Error, canonical, digest

SELECTORS = {'session','mail_instance_id','mail_agent_id'}
DISPLAY_FORMAT = '#{session_id}\t#{pane_id}\t#{pane_pid}\t#{pane_dead}\t#{pane_current_command}'


@dataclass
class Selected:
    client: FreshBoundClient
    name: str
    run: Path
    intent: dict
    receipt: dict
    ended: dict | None
    route: dict | None
    signature: str

    def public(self):
        return dict(mail_instance_id=self.client.context['mail_instance_id'],
                    mail_agent_id=self.client.context['mail_agent_id'],session=self.name)


class Targets:
    def __init__(self, operator, root, *, runtime=None):
        self.operator = operator if isinstance(operator,FreshBoundClient) else FreshBoundClient(operator)
        self.root = root_path(root)
        self.runtime = runtime
        self._membership = {}

    @asynccontextmanager
    async def _runtime(self, context):
        runtime = self.runtime
        owned = runtime is None
        if owned:
            runtime = await FreshRuntime.open(Path(context['root'])/'config.json')
        try:
            if runtime.root != Path(context['root']) or runtime.config['instance_id'] != context['mail_instance_id']:
                raise B1Error('FRESH_CLIENT_CONFIG_MISMATCH')
            yield runtime
        finally:
            if owned:
                await runtime.close()

    async def select(self, query):
        return await self._select(query, managed=False)

    async def select_managed(self, query):
        """Trusted host only; ordinary model/notification authentication stays active."""
        return await self._select(query, managed=True)

    async def _select(self, query, *, managed):
        if not isinstance(query,dict) or set(query)-SELECTORS:raise B1Error('FRESH_TARGET_QUERY_INVALID')
        context,proof=self.operator._load()
        instance=context['mail_instance_id']
        if query.get('mail_instance_id',instance)!=instance:raise B1Error('INSTANCE_MISMATCH')
        aid=query.get('mail_agent_id');name=query.get('session')
        if aid is not None:
            if ('mail_instance_id' not in query or isinstance(aid,bool) or not str(aid).isdigit() or int(aid)<=0):
                raise B1Error('FRESH_TARGET_QUERY_INVALID')
            aid=int(aid)
        elif not isinstance(name,str) or not name:raise B1Error('TARGET_SELECTOR_REQUIRED')
        async with self._runtime(context) as runtime:
          try:
            async with runtime.core.owner_transaction(instance,proof) as owner:
                if len(owner.records)>5000:raise B1Error('FRESH_ROSTER_LIMIT')
                row=await runtime.core._resolve(owner.session,owner.state,owner.records,
                    target_agent_id=aid,target_name=name if aid is None else None,
                    expected_target_name=name if aid is not None else None, active=not managed)
                folder=Path(context['root'])/'clients';_directory(folder)
                entries=list(folder.iterdir())
                if len(entries)>5000:raise B1Error('FRESH_ROSTER_LIMIT')
                matches=[]
                for entry in entries:
                    try:
                        native_id(entry.name);_directory(entry)
                        client=FreshBoundClient(entry/'context.json')
                        if client.context['root']==context['root'] and client.context['mail_instance_id']==instance and client.context['mail_agent_id']==row.id:
                            matches.append(client)
                    except (B1Error,OSError):continue
                if len(matches)!=1:raise B1Error('FRESH_TARGET_CONTEXT_UNAVAILABLE')
                client=matches[0];_,target_proof=client._load()
                verified=(runtime.core.host_target(owner,target_proof,row.id) if managed else
                          runtime.core._verify(target_proof,owner.records,owner.keys))
                if verified.id!=row.id:
                    raise B1Error('FRESH_TARGET_BINDING_MISMATCH')
                # No mkdir/repair: an unlaunched target is unavailable, not an
                # invitation to create a run or substitute the Desktop template.
                base=client.path.parent/'launch';_directory(base)
                current=_read_private(base/'current.json')
                if set(current)!={'launch_id'}:raise B1Error('FRESH_TMUX_TARGET_CHANGED')
                run=base/'runs'/native_id(current['launch_id']);_directory(run)
                intent=_read_private(run/'intent.json');receipt=_read_private(run/'session.json')
                for value in (intent,receipt):pinned(client,value)
                if (any(v.get('launch_id')!=current['launch_id'] for v in (intent,receipt)) or
                        receipt.get('provider')!=intent.get('provider') or intent.get('wrapper_root')!=str(self.root)):
                    raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
                provider=intent['provider']
                if provider in {'dsh','hermes'}:
                    from .fresh_harness import native_session_id
                    sid=native_session_id(receipt.get('native_session_id'))
                elif provider in {'claude','codex'}:sid=native_id(receipt.get('native_session_id'))
                else:raise B1Error('FRESH_TARGET_PROVIDER_UNAVAILABLE')
                if receipt!=dict(_read_private(base/'sessions'/(sid+'.json')),launch_id=current['launch_id']):
                    raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
                ended=_read_private(run/'ended.json') if (run/'ended.json').exists() else None
                if ended is not None and (set(ended)!={'launch_id','exit_code'} or ended['launch_id']!=current['launch_id'] or type(ended['exit_code']) is not int):
                    raise B1Error('FRESH_LAUNCH_OUTCOME_UNKNOWN')
                route=_read_private(run/'tmux.json') if (run/'tmux.json').exists() else None
                if route is not None:
                    pinned(client,route)
                    if route.get('kind')!='orrery-fresh-tmux-route-v1' or route.get('launch_id')!=current['launch_id']:
                        raise B1Error('FRESH_TMUX_TARGET_CHANGED')
                signature=digest(dict(context=client.context_digest,binding=client.context['binding_digest'],
                    identity=identity(client),current=current,intent=intent,receipt=receipt,ended=ended,route=route))
                return Selected(client,row.name,run,intent,receipt,ended,route,signature)
          except B1Error:raise
          except (OSError,ValueError,TypeError,KeyError):
              raise B1Error('FRESH_TARGET_UNAVAILABLE') from None

    async def again(self, selected):
        current=await self.select({k:v for k,v in selected.public().items() if k!='session'})
        if current.signature!=selected.signature:raise B1Error('FRESH_TMUX_TARGET_CHANGED')
        return current

    async def authorize(self, selected):
        context,proof=self.operator._load()
        async with self._runtime(context) as runtime:
            async with runtime.core.owner_transaction(context['mail_instance_id'],proof) as owner:
                target_context,target_proof=selected.client._load()
                if (target_context['mail_instance_id']!=context['mail_instance_id'] or
                        runtime.core._verify(target_proof,owner.records,owner.keys).id!=selected.client.context['mail_agent_id']):
                    raise B1Error('FRESH_TARGET_BINDING_MISMATCH')

    def pane(self, selected, *, readiness=False):
        if selected.route is None:raise B1Error('FRESH_TARGET_HAS_NO_PANE')
        if selected.ended is None:
            return delivery.target(selected.client,self.root,selected.route,readiness=readiness)
        # Ended is a distinct capability. Never weaken notification delivery's
        # live runner/provider checks to make a husk look like an active native.
        row=selected.route
        tmux,processes,_=delivery.helpers(self.root)
        try:
            transport=tmux['Transport'](Path(row['socket_path']))
            if list(transport.identity)!=row['socket_identity']:raise B1Error('FRESH_TMUX_TARGET_CHANGED')
            actual=tmux['panes'](transport,pane=row['pane']['pane_id'])
            if len(actual)!=1 or any(actual[0][k]!=v for k,v in row['pane'].items()):
                raise B1Error('FRESH_TMUX_TARGET_CHANGED')
            if delivery.require_birth(processes,row['pane']['pid'])!=row['pane_process_start']:
                raise B1Error('FRESH_TMUX_TARGET_CHANGED')
            if (not processes['belongs_to'](row['pane']['pid'],row['pane']['pid'],os.getuid()) or
                    delivery.require_birth(processes,row['pane']['pid'])!=row['pane_process_start']):
                raise B1Error('FRESH_TMUX_TARGET_CHANGED')
            return transport,row
        except tmux['TargetError'] as exc:raise B1Error(str(exc)) from None

    def public_text(self, selected, text, limit=262144):
        # Structural private paths/owner credentials are not terminal/history
        # metadata. Do not echo them even if a provider printed the current K.
        hidden=[]
        for client in (self.operator,selected.client):
            context,proof=client._load()
            hidden.extend([proof['registration_token'],str(client.path),context['binding_path']])
        hidden.append(selected.receipt.get('transcript_path',''))
        for value in sorted(set(hidden),key=len,reverse=True):
            if value:text=text.replace(value,'[private]')
        raw=text.encode()
        return raw[:limit].decode('utf-8',errors='ignore'),len(raw)>limit

    async def capture(self, query, lines=500):
        selected=await self.select(query)
        transport,row=self.pane(selected)
        result=transport.command('capture-pane','-e','-p','-J','-t',row['pane']['pane_id'],
                                 '-S','-'+str(max(40,min(4000,int(lines)))))
        if result.returncode:raise B1Error('FRESH_TMUX_CAPTURE_FAILED')
        await self.again(selected);self.pane(selected)
        text,truncated=self.public_text(selected,result.stdout)
        return dict(ok=True,content=text,truncated=truncated,**selected.public())

    def transcript(self, selected):
        from .fresh_child import provider_home
        provider=selected.intent['provider']
        if provider not in {'claude','codex'}:raise B1Error('FRESH_HISTORY_PROVIDER_UNAVAILABLE')
        path=Path(selected.receipt['transcript_path'])
        home=provider_home(selected.client,provider)
        if (not path.is_absolute() or path.is_symlink() or
                not path.resolve(strict=True).is_relative_to(home.resolve(strict=True)) or not path.is_file()):
            raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
        if provider=='codex':
            import runpy
            reader=runpy.run_path(str(self.root/'hooks/prepare-codex-session-binding.py'))
            if reader['_transcript_header_session_id'](path)!=selected.receipt['native_session_id']:
                raise B1Error('FRESH_NATIVE_SESSION_MISMATCH')
        return path

    async def history(self, query, limit, read_events):
        selected=await self.select(query)
        path=self.transcript(selected)
        # The caller supplies the existing fixed parser, never a browser path.
        events,more=read_events(path,selected.intent['provider'],max(1,min(200,int(limit))))
        projected=[];budget=262144;truncated=False
        for event in events:
            if event.get('role') not in {'user','assistant'} or event.get('kind')!='text':continue
            text,cut=self.public_text(selected,event['text'],min(65536,budget))
            if not budget:truncated=True;break
            budget-=len(text.encode());truncated|=cut
            projected.append(dict(role=event['role'],kind='text',text=text,ts=str(event.get('ts',''))[:128]))
        await self.again(selected)
        return dict(ok=True,source=selected.intent['provider'],events=projected,total=len(projected),
            shown=len(projected),history_window='tail',has_more=bool(more or truncated),truncated=truncated,
            history_binding='bound',history_binding_reason='',**selected.public())

    async def catalog(self):
        context,proof=self.operator._load()
        async with self._runtime(context) as runtime:
            async with runtime.core.owner_transaction(context['mail_instance_id'],proof) as owner:
                if len(owner.records)>5000:raise B1Error('FRESH_ROSTER_LIMIT')
                active=[a for a in sorted(owner.records.values(),key=lambda a:a.id) if a.retired_at is None]
                return dict(ok=True,mail_instance_id=context['mail_instance_id'],truncated=len(active)>128,
                    agents=[dict(mail_instance_id=context['mail_instance_id'],mail_agent_id=a.id,session=a.name)
                            for a in active[:128]])

    async def inspect(self, query, *, purpose='attach', expected=None):
        if purpose not in {'attach','capture','input'}:raise B1Error('FRESH_TARGET_QUERY_INVALID')
        started=time.monotonic()
        selected=await self.select(query)
        if expected is not None and selected.signature!=expected:raise B1Error('FRESH_TMUX_TARGET_CHANGED')
        if purpose=='input' and selected.ended is not None:raise B1Error('FRESH_TMUX_TARGET_ENDED')
        if self.runtime is None:
            transport,row=self.pane(selected,readiness=purpose=='input')
        else:
            transport,row=await asyncio.to_thread(self.pane,selected,readiness=purpose=='input')
        await self.again(selected)
        if self.runtime is not None and selected.ended is None:
            now=time.monotonic()
            self._membership={key:value for key,value in self._membership.items() if value[0]>now}
            if len(self._membership)>=128:self._membership.pop(next(iter(self._membership)))
            if started+5>now:self._membership[selected.signature]=(started+5,selected)
        # Host CLI only. PC/phone projections do not receive the socket/path,
        # generation, process facts, or a protected client path.
        return dict(ok=True,**selected.public(),credential_generation=selected.client.context['credential_generation'],
            context_digest=selected.client.context_digest,current=selected.intent['launch_id'],
            native_session_id=selected.receipt['native_session_id'],provider=selected.intent['provider'],
            ended=selected.ended is not None,revision=selected.signature,route=row,
            transport_argv=transport.argv)

    async def membership(self, query, expected):
        if self.runtime is None or not isinstance(expected,str):raise B1Error('FRESH_TARGET_QUERY_INVALID')
        cached=self._membership.get(expected)
        if cached is None or time.monotonic()>=cached[0]:
            self._membership.pop(expected,None);raise B1Error('FRESH_TARGET_PROOF_EXPIRED')
        selected=cached[1]
        try:
            if query!=selected.public():raise B1Error('FRESH_TMUX_TARGET_CHANGED')
            self.operator._load();selected.client._load()
            row,intent=delivery.route(selected.client)
            if row!=selected.route or intent!=selected.intent:raise B1Error('FRESH_TMUX_TARGET_CHANGED')
            tmux,_,_=delivery.helpers(self.root)
            transport=tmux['Transport'](Path(row['socket_path']))
            if list(transport.identity)!=row['socket_identity']:raise B1Error('FRESH_TMUX_TARGET_CHANGED')
            actual=await asyncio.to_thread(tmux['panes'],transport,pane=row['pane']['pane_id'])
            if len(actual)!=1 or any(actual[0][k]!=v for k,v in row['pane'].items()):
                raise B1Error('FRESH_TMUX_TARGET_CHANGED')
            if time.monotonic()>=cached[0]:raise B1Error('FRESH_TARGET_PROOF_EXPIRED')
            return dict(ok=True,**selected.public(),revision=expected)
        except (OSError,ValueError,KeyError,TypeError):
            self._membership.pop(expected,None)
            raise B1Error('FRESH_TMUX_TARGET_CHANGED') from None
        except Exception:
            self._membership.pop(expected,None)
            raise

    async def exit(self, query):
        selected=await self.select(query)
        if selected.ended is not None:return await self.cleanup(query,mode='tmux')
        transport,row=self.pane(selected,readiness=True)
        await self.again(selected)
        marker=selected.run/'pc-exit.json'
        record=dict(operation='exit',revision=selected.signature,execution='unknown',**selected.public())
        try:_write_private(marker,record)
        except FileExistsError:
            saved=_read_private(marker)
            if saved.get('revision')!=selected.signature:raise B1Error('FRESH_TMUX_TARGET_CHANGED')
            return dict(ok=False,execution='unknown',error='FRESH_EXIT_OUTCOME_UNKNOWN',**selected.public())
        buffer='orrery-exit-'+selected.intent['launch_id']
        try:
            await self.again(selected);transport,row=self.pane(selected,readiness=True)
            if transport.command('load-buffer','-b',buffer,'-',input='/exit').returncode:
                raise B1Error('FRESH_TMUX_LOAD_FAILED')
            await self.again(selected);transport,row=self.pane(selected,readiness=True)
            if transport.command('paste-buffer','-d','-p','-b',buffer,'-t',row['pane']['pane_id']).returncode:
                raise B1Error('FRESH_TMUX_PASTE_UNKNOWN')
            await self.again(selected);transport,row=self.pane(selected)
            if transport.command('send-keys','-t',row['pane']['pane_id'],'C-m').returncode:
                raise B1Error('FRESH_TMUX_SUBMIT_UNKNOWN')
            await self.again(selected);self.pane(selected)
            return dict(ok=True,actions=['exit-command-accepted'],execution='accepted',**selected.public())
        finally:
            try:transport.command('delete-buffer','-b',buffer)
            except (OSError,RuntimeError):pass

    @staticmethod
    def validate_display_route(route):
        try:
            if (route['kind']!='orrery-fresh-tmux-route-v1' or not isinstance(route['socket_path'],str)
                    or not Path(route['socket_path']).is_absolute() or type(route['socket_identity']) is not list
                    or len(route['socket_identity'])!=2 or any(type(v) is not int or v<0 for v in route['socket_identity'])
                    or set(route['pane'])!={'session_id','pane_id','pid'}
                    or not re.fullmatch(r'\$[0-9]+',route['pane']['session_id'])
                    or not re.fullmatch(r'%[0-9]+',route['pane']['pane_id'])
                    or type(route['pane']['pid']) is not int or route['pane']['pid']<1
                    or not isinstance(route['pane_process_start'],str) or not route['pane_process_start']):raise ValueError
            for role in ('runner','provider'):
                if (set(route[role])!={'pid','process_start'} or type(route[role]['pid']) is not int or route[role]['pid']<1
                        or not isinstance(route[role]['process_start'],str) or not route[role]['process_start']):raise ValueError
        except (KeyError,TypeError,ValueError):raise B1Error('FRESH_CLEANUP_OBSERVATION_UNAVAILABLE') from None

    @staticmethod
    def _inventory(transport, deadline):
        if time.monotonic()>=deadline:raise B1Error('FRESH_CLEANUP_OBSERVATION_UNAVAILABLE')
        panes = transport.command('list-panes','-a','-F',DISPLAY_FORMAT,deadline=deadline)
        if panes.returncode or time.monotonic()>=deadline:raise B1Error('FRESH_CLEANUP_OBSERVATION_UNAVAILABLE')
        clients = transport.command('list-clients','-F','#{session_id}',deadline=deadline)
        if panes.returncode or clients.returncode:
            raise B1Error('FRESH_CLEANUP_OBSERVATION_UNAVAILABLE')
        if len(panes.stdout.encode())>262144 or len(clients.stdout.encode())>65536:
            raise B1Error('FRESH_CLEANUP_OBSERVATION_UNAVAILABLE')
        rows=[];seen=set()
        for line in panes.stdout.splitlines():
            f=line.split('\t')
            if (len(f)!=5 or not re.fullmatch(r'\$[0-9]+',f[0]) or not re.fullmatch(r'%[0-9]+',f[1])
                    or not f[2].isdigit() or int(f[2])<1 or f[3] not in {'0','1'}
                    or (f[0],f[1]) in seen):raise B1Error('FRESH_CLEANUP_OBSERVATION_UNAVAILABLE')
            seen.add((f[0],f[1]));rows.append(dict(session_id=f[0],pane_id=f[1],pid=int(f[2]),dead=f[3]=='1',command=f[4]))
        attached=set(clients.stdout.splitlines())
        if any(not re.fullmatch(r'\$[0-9]+',value) for value in attached):
            raise B1Error('FRESH_CLEANUP_OBSERVATION_UNAVAILABLE')
        return rows,attached

    @staticmethod
    def _matches_process(snapshot, process):
        row=snapshot.get(process['pid'])
        return row is not None and row[1:]==(os.getuid(),process['process_start'])

    @staticmethod
    def _ended_view(selected, snapshot, inventory):
        """Read-only facts shared by display and full cleanup; never a cached grant."""
        route=selected.route
        if selected.ended is None or route is None:raise B1Error('FRESH_CLEANUP_NOT_ENDED')
        for role in ('runner','provider'):
            # PID reuse is also refusal, not evidence that our process exited.
            if route[role]['pid'] in snapshot:raise B1Error('FRESH_CLEANUP_PROCESS_PRESENT')
        rows,attached=inventory
        actual=[r for r in rows if r['pane_id']==route['pane']['pane_id']]
        if not actual:
            if route['pane']['pid'] in snapshot:raise B1Error('FRESH_CLEANUP_TARGET_CHANGED')
            return dict(category='gone',running=False,attached=route['pane']['session_id'] in attached)
        if len(actual)!=1 or any(actual[0][k]!=v for k,v in route['pane'].items()):
            raise B1Error('FRESH_CLEANUP_TARGET_CHANGED')
        row=actual[0];pid=row['pid'];process=snapshot.get(pid)
        if process is None:
            if not row['dead']:raise B1Error('FRESH_CLEANUP_TARGET_CHANGED')
        else:
            if process!=(process[0],os.getuid(),route['pane_process_start']) or row['command'] not in {'sh','bash','zsh','fish','dash'}:
                raise B1Error('FRESH_CLEANUP_TARGET_CHANGED')
            for child in snapshot:
                cursor=child;seen=set()
                while cursor in snapshot and cursor not in seen and cursor!=pid:
                    seen.add(cursor);cursor=snapshot[cursor][0]
                if child!=pid and cursor==pid:raise B1Error('FRESH_CLEANUP_PROCESS_PRESENT')
        return dict(category='finished',running=False,attached=route['pane']['session_id'] in attached)

    def cleanup_status(self, selected):
        tmux,processes,_=delivery.helpers(self.root);deadline=time.monotonic()+5
        before=processes['process_snapshot'](deadline=deadline)
        if before is None:raise B1Error('FRESH_CLEANUP_OBSERVATION_UNAVAILABLE')
        route=selected.route
        if route is None:raise B1Error('FRESH_TARGET_HAS_NO_PANE')
        self.validate_display_route(route)
        path=Path(route['socket_path']);transport=None
        try:
            path.lstat()
        except FileNotFoundError:
            inventory=([],set())
        else:
            transport=tmux['Transport'](path)
            if list(transport.identity)!=route['socket_identity']:raise B1Error('FRESH_CLEANUP_TARGET_CHANGED')
            inventory=self._inventory(transport,deadline)
            if tmux['validate_socket'](path)!=transport.identity:raise B1Error('FRESH_CLEANUP_TARGET_CHANGED')
        first=self._ended_view(selected,before,inventory)
        after=processes['process_snapshot'](deadline=deadline)
        if after is None or self._ended_view(selected,after,inventory)!=first:
            raise B1Error('FRESH_CLEANUP_TARGET_CHANGED')
        if transport is not None:
            final=self._inventory(transport,deadline)
            if tmux['validate_socket'](path)!=transport.identity:raise B1Error('FRESH_CLEANUP_TARGET_CHANGED')
            if self._ended_view(selected,after,final)!=first:raise B1Error('FRESH_CLEANUP_TARGET_CHANGED')
        elif path.exists() or path.is_symlink():raise B1Error('FRESH_CLEANUP_TARGET_CHANGED')
        return transport,first

    async def _managed_again(self, selected):
        actual=await self.select_managed({k:v for k,v in selected.public().items() if k!='session'})
        if actual.signature!=selected.signature:raise B1Error('FRESH_TMUX_TARGET_CHANGED')
        return actual

    def display_snapshot(self, selections, *, deadline):
        """Bounded display batch. Its facts are never accepted by an effect API."""
        if time.monotonic()>=deadline:return {}
        tmux,processes,_=delivery.helpers(self.root)
        before=processes['process_snapshot'](deadline=deadline)
        if before is None:return {}
        inventories={};identities={}
        for selected in selections:
            if time.monotonic()>=deadline:break
            route=selected.route
            if route is None:continue
            key=(route['socket_path'],tuple(route['socket_identity']))
            if key in inventories:continue
            if len(inventories)>=2:break
            try:
                path=Path(key[0]);identities[key]=None
                try:path.lstat()
                except FileNotFoundError:
                    inventories[key]=([],set());continue
                transport=tmux['Transport'](path)
                if transport.identity!=key[1]:raise B1Error('FRESH_CLEANUP_TARGET_CHANGED')
                inventories[key]=self._inventory(transport,deadline)
                if tmux['validate_socket'](path)!=transport.identity:raise B1Error('FRESH_CLEANUP_TARGET_CHANGED')
                identities[key]=transport.identity
            except (B1Error,OSError,RuntimeError,ValueError):inventories[key]=None
        if time.monotonic()>=deadline:return {}
        after=processes['process_snapshot'](deadline=deadline)
        if after is None or time.monotonic()>=deadline:return {}
        result={}
        for selected in selections:
            route=selected.route
            if route is None:continue
            key=(route['socket_path'],tuple(route['socket_identity']));inventory=inventories.get(key)
            if inventory is None:continue
            try:
                path=Path(key[0])
                if identities[key] is None:
                    if path.exists() or path.is_symlink():continue
                elif tmux['validate_socket'](path)!=identities[key]:continue
                if selected.ended is not None:
                    first=self._ended_view(selected,before,inventory)
                    if self._ended_view(selected,after,inventory)!=first:continue
                else:
                    actual=[r for r in inventory[0] if all(r[k]==v for k,v in route['pane'].items())]
                    if len(actual)!=1 or actual[0]['dead']:continue
                    expected=[dict(pid=route['pane']['pid'],process_start=route['pane_process_start']),route['runner'],route['provider']]
                    if not all(self._matches_process(s,p) for s in (before,after) for p in expected):continue
                    if not all(processes['snapshot_belongs_to'](s,route['runner']['pid'],route['pane']['pid'],os.getuid()) and
                        processes['snapshot_belongs_to'](s,route['provider']['pid'],route['runner']['pid'],os.getuid()) for s in (before,after)):continue
                    first=dict(category='running',running=True,attached=route['pane']['session_id'] in inventory[1])
                result[selected.client.context['mail_agent_id']]=first
            except (B1Error,OSError,RuntimeError,KeyError,TypeError):continue
        return result

    async def cleanup(self, query, *, mode='both'):
        if mode not in {'both','tmux','retire'}:raise B1Error('FRESH_CLEANUP_MODE_INVALID')
        selected=await self.select_managed(query)
        result=dict(ok=False,mode=mode,actions=[],effects={'retire':'not_requested' if mode=='tmux' else 'not_attempted',
                    'tmux':'not_requested' if mode=='retire' else 'not_attempted'},execution='unknown',**selected.public())
        marker=selected.run/('pc-cleanup-'+mode+'.json')
        def save():
            temp=marker.with_name('.cleanup-'+uuid4().hex)
            _write_private(temp,dict(revision=selected.signature,result=result));os.replace(temp,marker)
        with operation(selected.run.parent.parent):
            await self._managed_again(selected)
            if marker.exists():
                saved=_read_private(marker)
                if saved.get('revision')!=selected.signature:raise B1Error('FRESH_TMUX_TARGET_CHANGED')
                return saved['result']
            transport,view=self.cleanup_status(selected)
            if view['attached']:raise B1Error('FRESH_CLEANUP_ATTACHED')
            _write_private(marker,dict(revision=selected.signature,result=dict(result,error='FRESH_CLEANUP_OUTCOME_UNKNOWN')))
            try:
                if mode!='tmux':
                    context,proof=self.operator._load();_,target_proof=selected.client._load()
                    async with self._runtime(context) as runtime:
                        async with runtime.core.owner_transaction(context['mail_instance_id'],proof) as owner:
                            retired=await runtime.core.host_retire(owner,target_proof,selected.client.context['mail_agent_id'])
                    result['effects']['retire']='retired' if retired['changed'] else 'already_retired'
                    result['actions'].append(result['effects']['retire']);save()
                if mode!='retire':
                    await self._managed_again(selected)
                    transport,view=self.cleanup_status(selected)
                    if view['attached']:raise B1Error('FRESH_CLEANUP_ATTACHED')
                    await self._managed_again(selected)
                    if view['category']=='gone':result['effects']['tmux']='already_absent'
                    else:
                        result['effects']['tmux']='unknown';save()
                        command=transport.command('kill-pane','-t',selected.route['pane']['pane_id'])
                        if command.returncode:raise B1Error('FRESH_CLEANUP_OUTCOME_UNKNOWN')
                        await self._managed_again(selected)
                        _,after=self.cleanup_status(selected)
                        if after['category']!='gone':raise B1Error('FRESH_CLEANUP_OUTCOME_UNKNOWN')
                        result['effects']['tmux']='pane_removed'
                    result['actions'].append(result['effects']['tmux'])
                result.update(ok=True,execution='completed')
            except (B1Error,OSError,RuntimeError) as exc:
                result['error']=exc.code if isinstance(exc,B1Error) else 'FRESH_CLEANUP_OUTCOME_UNKNOWN'
            save();return result

    async def jump(self, query, *, open_terminal=True, opener=None):
        selected=await self.select(query)
        transport,row=self.pane(selected)
        await self.again(selected)
        if selected.ended is None:
            if open_terminal:
                if opener is None:raise B1Error('FRESH_TERMINAL_ADAPTER_REQUIRED')
                # Revalidate inside the terminal child too; the fixed wrapper
                # and literal JSON are argv values, never shell source.
                request=canonical(dict(operation='attach',expected=selected.signature,**selected.public()))
                argv=['env','AGENTSTACK_RUNTIME_MODE=fresh','AGENTSTACK_FRESH_CLIENT_CONTEXT='+str(self.operator.path),
                    'AGENTSTACK_PYTHON='+sys.executable,str(self.root/'bin/agentstack-runtime-client'),'target-json','--request-json',request]
                result=opener(argv,selected.name)
                if not result.get('ok'):raise B1Error('FRESH_TERMINAL_OPEN_FAILED')
                await self.again(selected);self.pane(selected)
            return dict(ok=True,actions=['focus-current-pane'] if open_terminal else ['current-pane-ready'],
                        execution='active',**selected.public())
        cwd=selected.intent.get('cwd')
        if not isinstance(cwd,str) or not Path(cwd).is_dir():raise B1Error('FRESH_LAUNCH_ARGUMENTS_INVALID')
        self.transcript(selected) if selected.intent['provider'] in {'claude','codex'} else None
        from .fresh_launch import provider_setup
        provider_setup(selected.client,self.root,selected.intent['provider'])
        marker=selected.run/'pc-resume.json'
        gate='orrery-fresh-resume-'+uuid4().hex
        argv=['env','-u','AGENTSTACK_CLIENT_CONFIG','-u','AGENTSTACK_GLOBAL_SERVER_CONFIG',
            'AGENTSTACK_RUNTIME_MODE=fresh','AGENTSTACK_FRESH_CLIENT_CONTEXT='+str(selected.client.path),
            'AGENTSTACK_PYTHON='+sys.executable,str(self.root/'bin/agentstack-runtime-client'),'resume',
            selected.intent['provider'],cwd,'--session-id',selected.receipt['native_session_id']]
        with operation(selected.run.parent.parent):
            await self.again(selected);self.pane(selected)
            try:_write_private(marker,dict(revision=selected.signature,execution='unknown',gate=gate,**selected.public()))
            except FileExistsError:
                saved=_read_private(marker)
                if saved.get('revision')!=selected.signature:raise B1Error('FRESH_TMUX_TARGET_CHANGED')
                return dict(ok=False,execution='unknown',error='FRESH_RESUME_OUTCOME_UNKNOWN',**selected.public())
            await self.again(selected);transport,row=self.pane(selected)
            # Order final admission and -k against every regular launch writer.
            # The child waits at this unique existing tmux gate, so it cannot
            # reach fresh_launch's LOCK_NB while the host still holds the lock.
            gated=shlex.join([*transport.argv,'wait-for',gate])+' && exec '+shlex.join(argv)
            try:
                result=transport.command('respawn-pane','-k','-t',row['pane']['pane_id'],gated)
                if result.returncode:raise B1Error('FRESH_RESUME_OUTCOME_UNKNOWN')
            except (B1Error,OSError,RuntimeError):raise B1Error('FRESH_RESUME_OUTCOME_UNKNOWN') from None
        # ACK -> unlock -> current owner check -> signal. No provider wait is
        # held here; uncertain dispatch/signal keeps the marker and never retries.
        try:
            await self.authorize(selected)
            if transport.command('wait-for','-S',gate).returncode:
                raise B1Error('FRESH_RESUME_OUTCOME_UNKNOWN')
        except (B1Error,OSError,RuntimeError):raise B1Error('FRESH_RESUME_OUTCOME_UNKNOWN') from None
        if open_terminal and opener is not None:
            # The child rechecks the completed ended->resume transition. A raw
            # attach here would bypass the current owner/native/pane boundary.
            request=canonical(dict(operation='attach-resumed',resume_from=selected.intent['launch_id'],
                expected=selected.signature,**selected.public()))
            attached=opener(['env','AGENTSTACK_RUNTIME_MODE=fresh','AGENTSTACK_FRESH_CLIENT_CONTEXT='+str(self.operator.path),
                'AGENTSTACK_PYTHON='+sys.executable,str(self.root/'bin/agentstack-runtime-client'),
                'target-json','--request-json',request],selected.name)
            if not attached.get('ok'):raise B1Error('FRESH_RESUME_OUTCOME_UNKNOWN')
        return dict(ok=True,actions=['resume-command-accepted'],execution='unknown',native_ready=False,**selected.public())


async def execute(args, payload):
    if not isinstance(payload,dict):raise B1Error('FRESH_TARGET_QUERY_INVALID')
    operation_name=payload.get('operation')
    extras={'catalog':set(),'inspect':{'purpose','expected'},'capture':{'lines'},'attach':{'expected'},'attach-resumed':{'expected','resume_from'}}
    if operation_name not in extras:raise B1Error('FRESH_TARGET_OPERATION_UNAVAILABLE')
    if set(payload)-SELECTORS-{'operation'}-extras[operation_name]:raise B1Error('FRESH_TARGET_QUERY_INVALID')
    targets=Targets(args.context,args.wrapper_root);query={k:v for k,v in payload.items() if k in SELECTORS}
    if operation_name=='catalog':
        if query:raise B1Error('FRESH_TARGET_QUERY_INVALID')
        return await targets.catalog()
    if operation_name=='inspect':return await targets.inspect(query,purpose=payload.get('purpose','attach'),expected=payload.get('expected'))
    if operation_name=='capture':return await targets.capture(query,payload.get('lines',500))
    if operation_name in {'attach','attach-resumed'}:
        deadline=time.monotonic()+5
        while True:
            try:selected=await targets.select(query)
            except B1Error as exc:
                if (operation_name!='attach-resumed' or time.monotonic()>=deadline or
                        exc.code not in {'FRESH_TARGET_UNAVAILABLE','FRESH_PRIVATE_FILE_UNAVAILABLE'}):raise
                await asyncio.sleep(.1);continue
            if operation_name!='attach-resumed' or selected.ended is None:break
            if time.monotonic()>=deadline:raise B1Error('FRESH_RESUME_OUTCOME_UNKNOWN')
            await asyncio.sleep(.1)
        if operation_name=='attach':
            if selected.ended is not None or selected.signature!=payload.get('expected'):
                raise B1Error('FRESH_TMUX_TARGET_CHANGED')
        else:
            old=selected.run.parent/native_id(payload.get('resume_from'))
            old_intent=_read_private(old/'intent.json');old_receipt=_read_private(old/'session.json')
            for value in (old_intent,old_receipt):pinned(selected.client,value)
            marker=_read_private(old/'pc-resume.json');ended=_read_private(old/'ended.json')
            if (selected.ended is not None or marker.get('revision')!=payload.get('expected') or
                    any(marker.get(k)!=v for k,v in selected.public().items()) or
                    any(value.get('launch_id')!=old.name for value in (old_intent,old_receipt,ended)) or
                    set(ended)!={'launch_id','exit_code'} or type(ended['exit_code']) is not int or
                    ended.get('launch_id')!=old_intent['launch_id'] or
                    selected.intent.get('resume_session_id')!=old_receipt['native_session_id'] or
                    selected.receipt['native_session_id']!=old_receipt['native_session_id'] or
                    selected.intent['provider']!=old_intent['provider'] or selected.run==old):
                raise B1Error('FRESH_TMUX_TARGET_CHANGED')
        # A session can contain several agents/windows. Fully qualify the
        # window through this session and immutable pane ID, never its name
        # or current active pane; then select that pane before attaching.
        for command in ('select-window','select-pane'):
            await targets.again(selected);transport,row=targets.pane(selected)
            pane=row['pane']['pane_id']
            target=row['pane']['session_id']+':.'+pane if command=='select-window' else pane
            if transport.command(command,'-t',target).returncode:
                raise B1Error('FRESH_TMUX_FOCUS_FAILED')
        await targets.again(selected);transport,row=targets.pane(selected)
        os.execv(transport.binary,[*transport.argv,'attach-session','-t',row['pane']['session_id']])
    raise B1Error('FRESH_TARGET_OPERATION_UNAVAILABLE')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context',required=True);parser.add_argument('--wrapper-root',required=True)
    parser.add_argument('--request-json')
    args=parser.parse_args(argv)
    try:
        raw=args.request_json if args.request_json is not None else sys.stdin.read(16385)
        if len(raw.encode())>16384:raise B1Error('FRESH_TARGET_QUERY_INVALID')
        result=asyncio.run(execute(args,json.loads(raw)))
        print(canonical(result));return 0
    except (B1Error,OSError,ValueError,TypeError,KeyError) as exc:
        print(canonical(dict(ok=False,error=exc.code if isinstance(exc,B1Error) else 'FRESH_TARGET_UNAVAILABLE')))
        return 2


if __name__=='__main__':raise SystemExit(main())

