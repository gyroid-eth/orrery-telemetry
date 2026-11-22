"""Explicit target resolution over existing roots, credentials and live evidence."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def tmux_api():
    import runpy
    return runpy.run_path(str(Path(__file__).with_name('runtime_tmux.py')))


class Targets:
    def __init__(self, operator, *, api=None, transport_api=None, deadline=None):
        self.operator=operator
        self.api=operator.api if api is None else api
        self.tmux=tmux_api() if transport_api is None else transport_api
        self.deadline=time.monotonic()+5 if deadline is None else deadline
        self.observations=self.tmux['module_api']('runtime_observation')['Observations'](operator,lambda _:None,deadline=self.deadline)

    def fail(self,reason):raise self.tmux['TargetError'](reason)

    def root(self,aid):
        matches=[]
        parent=self.operator.clients_parent
        if not parent.exists():self.fail('TARGET_UNAVAILABLE')
        self.api['safe_private_directory'](parent)
        for root in sorted(parent.iterdir()):
            self.api['remaining'](self.deadline)
            self.api['validate_client_name'](root.name)
            self.api['safe_private_directory'](root)
            value=self.api['read_json'](root/self.api['CLIENT_LAYOUT']['context'])
            identity=value.get('identity')
            if isinstance(identity,dict) and identity.get('agent_id')==aid:matches.append(root)
        if len(matches)!=1:self.fail('GLOBAL_TARGET_OWNERSHIP_UNKNOWN' if matches else 'TARGET_UNAVAILABLE')
        return matches[0]

    def snapshot(self,aid, *, held_root=None):
        root=self.root(aid)
        value,digest=self.observations.snapshot(root,aid,lock_held=root==held_root)
        self.operator.read({'kind':'health'},deadline=self.deadline)
        reply=self.api['management_exchange'](self.operator.config,{'version':1,'action':'inspect',**self.operator.binding,'agent_id':aid},deadline=self.deadline)
        row=self.api['validate_owner_receipt'](reply,value['context']['identity'],value['credential'].get('registration_token'),self.operator.binding)
        return root,value,digest,row

    def principal(self,aid,target,context,operator,held_root=None):
        if operator:return dict(kind='operator',uid=os.getuid(),agent_id=None),None
        if context is None:self.fail('OWNER_REQUIRED')
        document=self.api['read_json'](context)
        caller=(document.get('identity') or {}).get('agent_id')
        if not self.api['integer'](caller,1):self.fail('OWNER_REQUIRED')
        root,value,digest,row=self.snapshot(caller,held_root=held_root)
        if root/self.api['CLIENT_LAYOUT']['context'] != context or row.get('retired_at'):self.fail('OWNER_REQUIRED')
        if caller==aid:return dict(kind='owner',uid=os.getuid(),agent_id=caller),digest
        parent=(target.get('child') or {}).get('parent')
        if parent!={'client_name':root.name,'server_instance_id':self.operator.binding['expected_server_instance_id'],'agent_id':caller}:self.fail('OWNER_REQUIRED')
        return dict(kind='parent',uid=os.getuid(),agent_id=caller),digest

    def endpoints(self,root,launch):
        return self.tmux['endpoint_candidates'](root.parent.parent,self.operator.binding['expected_server_instance_id'],launch)

    def standalone_sessions(self,value):
        """Ignore completed launch history, but never bypass pending cleanup."""
        launch=value['launch']
        if value.get('child'):return []
        if launch:
            if launch.get('phase') not in ('ended','cancelled'):return []
            if (launch.get('execution') or launch.get('termination')) and (launch.get('termination') or {}).get('phase')!='ended':return []
            if any(not x.get('ended') and x.get('launch_id')==launch['launch_id'] for x in value['lives']):return []
        return [x for x in value['lives'] if x.get('launch_id') is None]

    def lifecycle(self,root,value,digest,row,principal,parent_digest):
        launch=value['launch'];pending=value.get('mutation');retired=bool(row.get('retired_at'))
        standalone=self.standalone_sessions(value)
        session=None
        if standalone:
            # Saved slots identify the owner/session, never a current PID.
            # Completed slots identify history only, including for operators.
            # Every unresolved live or unreadable process keeps cleanup unavailable.
            absent=all(self.api['process_start'](x.get('pid'),deadline=self.deadline)=='absent'
                for x in value['lives'] if not x.get('ended'))
            session=next((x['session_id'] for x in standalone if not x.get('ended')),standalone[0]['session_id'])
            phase='ended' if all(x.get('ended') for x in standalone) and pending is None else 'cleanup-pending'
            outcome='exited' if absent else 'unknown'
            operations=['target']
            if pending is not None and pending.get('tool')!='release_file_reservations':operations.append('resolve-mutation')
            elif absent and phase!='ended':operations.append('replay-end')
            if not absent:phase='unknown'
            kind='standalone-lifecycle';reference=None
        else:
            if not launch:self.fail('TMUX_TARGET_UNAVAILABLE')
            term=launch.get('termination') or {};phase=term.get('phase','unknown')
            outcome=launch.get('execution',{}).get('outcome',{}).get('state','unknown')
            other=[x for x in value['lives'] if not x.get('ended') and x.get('launch_id')!=launch['launch_id']]
            if any(x.get('receipt_nonce')!=launch['receipt_nonce'] for x in value['lives'] if x.get('launch_id')==launch['launch_id']):self.fail('LAUNCH_RECEIPT_MISMATCH')
            if other:phase='unknown'
            if retired and pending is not None and phase in ('report-pending','report-required'):operations=['target','resolve-mutation']
            elif not other and (outcome in ('exited','not-started') or (launch.get('operator_resolution') or {}).get('action')=='confirmed-ended'):
                operations=['target']+([] if phase=='ended' else ['replay-end'])
            else:operations=['target','launch-resolve']
            kind='launch-lifecycle';reference={k:launch[k] for k in ('launch_id','receipt_nonce')}
        target=dict(agent_id=row['agent_id'],agent_name=row['name'],credential_generation=row['credential_generation'],binding=dict(self.operator.binding),
            pid=None,process_start=None,transport=None,socket_path=None,server_pid=None,session_id=None,session_name=None,window_id=None,pane_id=None,
            argv=[],environment_unset=['TMUX','TMUX_PANE'],kind=kind,launch=reference)
        readiness=dict(state='unknown',input_empty=None,context_used_percent=None)
        state=self.tmux['target_digest'](target,principal,{'target':digest,'parent':parent_digest},readiness)
        return dict(root=root,target=target,principal=principal,identity_sha256=state,state_sha256=state,
            compact_readiness=readiness,next_operations=operations,transport=None,retired=retired,
            lifecycle={'phase':phase,'provider_outcome':outcome,'pending_sha256':value.get('mutation_sha256')},snapshot=value,lifecycle_session=session)

    def resolve(self,aid,*,context=None,operator=False,held_root=None):
        root,value,digest,row=self.snapshot(aid,held_root=held_root)
        principal,parent_digest=self.principal(aid,value,context,operator,held_root)
        launch=value['launch'];matches=[];uncertain=False;observed_live=False
        standalone=(any(not live.get('ended') and live.get('launch_id') is None for live in value['lives'])
            or bool(self.standalone_sessions(value)))
        if launch and not standalone and ((launch.get('execution') or {}).get('outcome',{}).get('state') in ('exited','not-started','unknown') or (launch.get('termination') or {}).get('phase') in ('cleanup-pending','ended')):
            return self.lifecycle(root,value,digest,row,principal,parent_digest)
        metadata_path=root/self.api['CLIENT_LAYOUT']['metadata']
        metadata=self.api['read_json'](metadata_path) if metadata_path.is_file() else value['context'].get('registration_metadata',{})
        if self.tmux['module_api']('runtime_observation')['unproven_app'](value,metadata):self.fail('TMUX_TARGET_UNAVAILABLE')
        for live in value['lives']:
            if live.get('ended'):continue
            start=self.api['process_start'](live.get('pid'),deadline=self.deadline)
            if start=='absent':continue
            if not start or start!=live.get('process_start'):uncertain=True;continue
            observed_live=True
            live_launch=self.tmux['launch_for_live'](live,launch)
            pane=live.get('pane_id')
            if not pane:
                matches.append((live,None,None,live_launch));continue
            for kind,transport in self.endpoints(root,live_launch):
                try:panes=self.tmux['panes'](transport,pane=pane,deadline=self.deadline)
                except self.tmux['TargetError'] as exc:
                    if str(exc)=='TMUX_TARGET_UNAVAILABLE':continue
                    raise
                for actual in panes:
                    if actual['pid']!=self.tmux['pane_process'](live,live_launch):continue
                    if not self.tmux['runner_provider'](live,live_launch,api=self.api,deadline=self.deadline):continue
                    if live_launch:
                        expected=(row['name'] if live_launch.get('tmux_transport')=='global-instance' else 'orrery-'+live_launch['launch_id'])
                        if actual['session']!=expected:self.fail('TMUX_TARGET_CHANGED')
                    server=transport.command('display-message','-p','#{pid}',deadline=self.deadline)
                    if server.returncode or not server.stdout.strip().isdigit():self.fail('TMUX_TARGET_UNAVAILABLE')
                    transport.server_pid=int(server.stdout.strip())
                    matches.append((live,(kind,transport),actual,live_launch))
        if uncertain or len(matches)>1:self.fail('GLOBAL_TARGET_OWNERSHIP_UNKNOWN')
        if not matches:
            if observed_live:self.fail('TMUX_TARGET_UNAVAILABLE')
            return self.lifecycle(root,value,digest,row,principal,parent_digest)
        match=matches[0]
        live,endpoint,actual=match[:3]
        launch=match[3] if len(match)==4 else None
        target=dict(agent_id=aid,agent_name=row['name'],credential_generation=row['credential_generation'],binding=dict(self.operator.binding),pid=live['pid'],process_start=live['process_start'],transport=None,socket_path=None,server_pid=None,session_id=None,session_name=None,window_id=None,pane_id=None,argv=[],environment_unset=['TMUX','TMUX_PANE'],kind='standalone-process',launch=None)
        text=None;provider=launch.get('provider') if launch else None
        if endpoint:
            kind,transport=endpoint
            window=transport.command('display-message','-p','-t',actual['pane_id'],'#{window_id}',deadline=self.deadline)
            if window.returncode or not re.fullmatch(r'@[0-9]+',window.stdout.strip()):self.fail('TMUX_TARGET_UNAVAILABLE')
            capture=transport.command('capture-pane','-p','-t',actual['pane_id'],'-S','-45',deadline=self.deadline)
            if capture.returncode:self.fail('TMUX_TARGET_UNAVAILABLE')
            text=capture.stdout
            target.update(transport=kind,socket_path=str(transport.path),server_pid=transport.server_pid,session_id=actual['session_id'],session_name=actual['session'],window_id=window.stdout.strip(),pane_id=actual['pane_id'],argv=[*transport.argv,'capture-pane','-p','-t',actual['pane_id'],'-S','-40'],kind='launched-tmux' if launch else 'standalone-tmux',launch={'launch_id':launch['launch_id'],'receipt_nonce':launch['receipt_nonce']} if launch else None)
            if provider is None:
                metadata=root/self.api['CLIENT_LAYOUT']['metadata']
                # Metadata is the existing registration truth; no provider guess
                # from terminal prose or an extra provider field is persisted.
                key=self.api['CLIENT_LAYOUT'].get('metadata')
                if key:metadata=root/key
                if metadata.is_file():
                    program=self.api['read_json'](metadata).get('program')
                    provider={'claude-code':'claude','codex':'codex','codex-cli':'codex'}.get(program)
        if endpoint:
            self.tmux['verify_live_pane'](transport,live,actual,api=self.api,deadline=self.deadline,launch=launch)
        else:
            self.tmux['verify_live_process'](live,api=self.api,deadline=self.deadline)
        readiness=self.tmux['compact_readiness'](text or '',provider)
        operations=['target']+(['capture'] if endpoint else [])
        if endpoint and operator and not row.get('retired_at'):operations.append('attach')
        if endpoint and not row.get('retired_at') and principal['kind']!='owner' and not self.tmux['compact_refusal'](readiness):operations.append('send-compact')
        if not row.get('retired_at') and (endpoint or provider=='gemini'):operations.append('end')
        identity=self.tmux['identity_digest'](target,principal,{'target':digest,'parent':parent_digest})
        state=self.tmux['target_digest'](target,principal,{'target':digest,'parent':parent_digest},readiness,screen=text,provider=provider)
        return dict(root=root,target=target,principal=principal,identity_sha256=identity,state_sha256=state,compact_readiness=readiness,next_operations=operations,transport=endpoint[1] if endpoint else None,retired=bool(row.get('retired_at')),snapshot=value,live=live,pane=actual,launch=launch)

    def response(self,resolved,operation,**extra):
        keys=('principal','target','state_sha256','next_operations') + (() if operation=='attach' else ('compact_readiness',))
        value={key:resolved[key] for key in keys}
        if operation in ('capture','attach'):
            value['state_sha256']=resolved['identity_sha256']
        if 'lifecycle' in resolved:value['lifecycle']=resolved['lifecycle']
        return dict(contract=self.tmux['CONTRACT'],ok=True,allowed=True,operation=operation,**value,**extra)

    def execute(self,operation,aid,*,context=None,operator=False,lines=40,read_only=False,print_only=False,expected=None,confirm=None,exec_fn=None,expected_intent=None):
        if operation=='attach' and not operator:self.fail('OPERATOR_CONFIRMATION_REQUIRED')
        resolved=self.resolve(aid,context=context,operator=operator)
        if operation=='target':return self.response(resolved,operation),'target.lifecycle.success' if 'lifecycle' in resolved else 'target.success'
        if operation in ('end','replay-end'):return self.end(resolved,operation,aid,context,operator,expected,confirm,expected_intent)
        transport=resolved['transport']
        if transport is None:self.fail('TMUX_TARGET_UNAVAILABLE')
        if operation=='capture':
            if not self.api['integer'](lines,1) or lines>1000:self.fail('TMUX_TARGET_INPUT_INVALID')
            # An extra line is evidence for truncation, never silently returned.
            again=self.resolve(aid,context=context,operator=operator)
            if again['identity_sha256']!=resolved['identity_sha256']:self.fail('TMUX_TARGET_CHANGED')
            capture=transport.command('capture-pane','-p','-t',resolved['target']['pane_id'],'-S','-'+str(lines+1),deadline=self.deadline)
            if capture.returncode:self.fail('TMUX_TARGET_UNAVAILABLE')
            target=resolved['target']
            actual={key:target[key] for key in ('pid','pane_id','session_id')}
            actual['session']=target['session_name']
            self.tmux['verify_live_pane'](transport,again['live'],again['pane'],api=self.api,deadline=self.deadline,launch=again.get('launch'))
            rows=capture.stdout.splitlines();return self.response(resolved,operation,capture=dict(text='\n'.join(rows[-lines:]),truncated=len(rows)>lines,max_lines=lines)),'capture.success'
        if operation=='attach':
            if resolved['retired']:self.fail('TARGET_UNAVAILABLE')
            argv=[*transport.argv,'attach-session','-t',resolved['target']['session_id']]
            if read_only:argv.append('-r')
            resolved['target']={**resolved['target'],'argv':argv}
            reply=self.response(resolved,operation,phase='admitted')
            if print_only:return reply,'attach.admission'
            # No mutex is held during a terminal lifetime.
            current=self.resolve(aid,context=context,operator=operator)
            if current['retired']:self.fail('TARGET_UNAVAILABLE')
            if current['identity_sha256']!=resolved['identity_sha256']:self.fail('TMUX_TARGET_CHANGED')
            self.emit(reply,'attach.admission',stream=sys.stderr)
            env={k:v for k,v in os.environ.items() if k not in ('TMUX','TMUX_PANE')}
            (os.execvpe if exec_fn is None else exec_fn)(argv[0],argv,env)
            return None,None
        if resolved['retired']:self.fail('TARGET_UNAVAILABLE')
        if resolved['principal']['kind']=='owner':self.fail('TMUX_TARGET_NOT_IDLE')
        if operator and (confirm is None or not confirm()):self.fail('OPERATOR_CONFIRMATION_REQUIRED')
        # Human confirmation is outside the observation budget and mutex.
        if operator:
            self.deadline=time.monotonic()+5
            self.observations.deadline=self.deadline
        with self.api['observation_inode'](resolved['root']/'runtime/child.lock',exclusive=True):
            current=self.resolve(aid,context=context,operator=operator,held_root=resolved['root'])
            if current['retired']:self.fail('TARGET_UNAVAILABLE')
            refusal=self.tmux['compact_refusal'](current['compact_readiness'])
            if refusal:self.fail(refusal)
            if current['state_sha256']!=expected:self.fail('RESOLUTION_DIGEST_MISMATCH')
            pane=current['target']['pane_id'];channel=current['transport']
            handed=False
            try:
                # Literal input, no prompt erasure and no automatic resend.
                handed=True
                result=channel.command('send-keys','-t',pane,'-l','/compact',deadline=self.deadline)
                if result.returncode:self.fail('TMUX_OPERATION_OUTCOME_UNKNOWN')
                result=channel.command('send-keys','-t',pane,'C-m',deadline=self.deadline)
                if result.returncode:self.fail('TMUX_OPERATION_OUTCOME_UNKNOWN')
            except (OSError,RuntimeError,ValueError,subprocess.SubprocessError):
                if handed:self.fail('TMUX_OPERATION_OUTCOME_UNKNOWN')
                raise
            return self.response(current,operation,phase='submitted'),'compact.submitted'

    def end(self,resolved,operation,aid,context,operator,expected,confirm,expected_intent):
        if operator and (confirm is None or not confirm()):self.fail('OPERATOR_CONFIRMATION_REQUIRED')
        self.deadline=time.monotonic()+5;self.observations.deadline=self.deadline
        client=self.api['RuntimeClient'](resolved['root']/self.api['CLIENT_LAYOUT']['context'])
        with client.operation_mutex():
            current=self.resolve(aid,context=context,operator=operator,held_root=resolved['root'])
            if current['state_sha256']!=expected:self.fail('RESOLUTION_DIGEST_MISMATCH')
            if operation=='replay-end':
                if 'lifecycle' not in current or operation not in current['next_operations']:self.fail('TMUX_TARGET_UNAVAILABLE')
                if current['lifecycle']['pending_sha256']!=expected_intent:self.fail('MUTATION_PENDING_CONFLICT')
                lives=current['snapshot']['lives']
                session=current.get('lifecycle_session') or next((x['session_id'] for x in lives if not x.get('ended')),None)
                client._lifecycle_principal=current['principal']
                try:client.end_session({'session_id':session})
                except (RuntimeError,OSError):self.fail('TMUX_OPERATION_OUTCOME_UNKNOWN')
            else:
                if 'lifecycle' in current:self.fail('TMUX_TARGET_UNAVAILABLE')
                # Signal only the birth/UID-proven provider. Never its runner or
                # a process group with unrelated descendants.
                live=current['live']
                if not self.tmux['runner_provider'](live,current.get('launch'),api=self.api,deadline=self.deadline):self.fail('TMUX_TARGET_CHANGED')
                stream=(current.get('launch') or {}).get('provider')=='gemini'
                channel=current['transport']
                if not stream and channel is None:self.fail('TMUX_TARGET_UNAVAILABLE')
                def send_exit(*keys):
                    self.tmux['verify_live_pane'](channel,live,current['pane'],api=self.api,deadline=self.deadline,launch=current.get('launch'))
                    result=channel.command('send-keys','-t',current['target']['pane_id'],*keys,deadline=self.deadline)
                    if result.returncode:self.fail('TMUX_OPERATION_OUTCOME_UNKNOWN')
                try:
                    self.api['request_provider_exit'](send=send_exit,
                        interrupt=lambda signum:os.kill(live['pid'],signum),
                        delegated_antigravity=stream)
                except (OSError,subprocess.SubprocessError):self.fail('TMUX_OPERATION_OUTCOME_UNKNOWN')
                self.fail('TMUX_OPERATION_OUTCOME_UNKNOWN')
            self.deadline=time.monotonic()+5;self.observations.deadline=self.deadline
            try:
                # A previously authorized owner may retire itself in this very
                # operation. Return that operation's verified completion, without
                # granting a retired owner a new target/read admission.
                final=None
                if current['principal']['kind']=='owner' and operation=='replay-end':
                    root,value,digest,row=self.snapshot(aid,held_root=resolved['root'])
                    launch=value['launch']
                    expected_launch=current['target'].get('launch')
                    if (row.get('retired_at') and launch and expected_launch
                            and all(launch.get(key)==expected_launch[key] for key in ('launch_id','receipt_nonce'))
                            and (launch.get('termination') or {}).get('phase')=='ended'):
                        final=self.lifecycle(root,value,digest,row,current['principal'],digest)
                        final['next_operations']=[]
                if final is None:final=self.resolve(aid,context=context,operator=operator,held_root=resolved['root'])
            except (RuntimeError,OSError,ValueError,KeyError,TypeError):self.fail('TMUX_OPERATION_OUTCOME_UNKNOWN')
            return self.response(final,operation),operation+'.success'

    def emit(self,value,name,stream=None):
        self.api['wire_contract']()['validate_schema'](value,self.tmux['target_result_schema'](name))
        print(json.dumps(value,ensure_ascii=False),file=sys.stdout if stream is None else stream,flush=True)


def main(argv,*,api=None):
    tmux=tmux_api();operation=argv[0] if argv else 'target'
    if tmux['TARGET_SPEC'] is None:
        print('TMUX_HELPER_FAILED',file=sys.stderr);return 70
    try:
        class Parser(argparse.ArgumentParser):
            def error(self,message):raise tmux['TargetError']('TMUX_TARGET_INPUT_INVALID')
        parser=Parser()
        parser.add_argument('operation',choices=['target','capture','attach','send-compact','end','replay-end'])
        parser.add_argument('--agent-id',type=int,required=True)
        parser.add_argument('--operator',action='store_true')
        parser.add_argument('--context');parser.add_argument('--runtime-mode');parser.add_argument('--server-config')
        parser.add_argument('--print',dest='print_only',action='store_true')
        parser.add_argument('--read-only',action='store_true');parser.add_argument('--lines',type=int,default=40)
        parser.add_argument('--confirm-state-digest')
        parser.add_argument('--expected-intent-sha256')
        args=parser.parse_args(argv)
        if args.agent_id<1 or not 1<=args.lines<=1000 or (args.operation=='send-compact' and (args.confirm_state_digest is None or re.fullmatch(r'[a-f0-9]{64}',args.confirm_state_digest) is None)):raise tmux['TargetError']('TMUX_TARGET_INPUT_INVALID')
        api=tmux['module_api']('runtime_client') if api is None else api
        request={'entry':args.operation,'agent_id':args.agent_id}
        if args.operation in ('target','attach'):request['print']=args.print_only
        if args.operation=='capture':request['lines']=args.lines
        if args.operation=='attach':request['read_only']=args.read_only
        if args.operation=='send-compact':request['confirm_state_digest']=args.confirm_state_digest
        if args.operation in ('end','replay-end'):request['confirm_state_digest']=args.confirm_state_digest
        if args.operation=='replay-end':request['expected_intent_sha256']=None if args.expected_intent_sha256=='null' else args.expected_intent_sha256
        if (args.read_only and args.operation!='attach') or (args.print_only and args.operation not in ('target','attach')):
            raise tmux['TargetError']('TMUX_TARGET_INPUT_INVALID')
        api['wire_contract']()['validate_schema'](request,tmux['TARGET_SPEC']['input_schemas'][args.operation],'TMUX_TARGET_INPUT_INVALID')
        if args.operator:
            api['wire_contract']()['validate_schema']({'operator':True,'runtime_mode':args.runtime_mode,'server_config':args.server_config},tmux['TARGET_SPEC']['input_schemas']['operator_admission'],'TMUX_TARGET_INPUT_INVALID')
        choice=api['selected'](argv)
        if choice['mode']!='global':raise tmux['TargetError']('OWNER_REQUIRED')
        context=choice['context']
        config=choice['server_config']
        if config is None and context is not None:config=api['absolute'](api['read_json'](context)['runtime_root'])/'server-config.json'
        if config is None:raise tmux['TargetError']('OWNER_REQUIRED')
        operator=tmux['module_api']('runtime_operator')['RuntimeOperator'](config,api=api)
        targets=Targets(operator,api=api,transport_api=tmux)
        def confirm():
            prompt={'end':'Request this provider to exit? Type yes: ',
                    'replay-end':'Finish this saved terminal report and cleanup? Type yes: '}.get(args.operation,'Submit fixed /compact to this target? Type yes: ')
            if not sys.stdin.isatty():return False
            print(prompt,end='',file=sys.stderr,flush=True)
            return input('').strip()=='yes'
        value,name=targets.execute(args.operation,args.agent_id,context=context,operator=args.operator,lines=args.lines,read_only=args.read_only,print_only=args.print_only,expected=args.confirm_state_digest,confirm=confirm,expected_intent=None if args.expected_intent_sha256=='null' else args.expected_intent_sha256)
        if value is not None:targets.emit(value,name)
        return 0
    except (RuntimeError,OSError,ValueError,KeyError,TypeError) as exc:
        reason=tmux['target_reason'](exc);kind=tmux['TARGET_REASONS'][reason]
        value=dict(contract=tmux['CONTRACT'],ok=False,allowed=False,operation=operation,failure_kind=kind,reason=reason,effects='unknown' if kind=='outcome' else 'none',next_operations=(['target'] if operation in ('end','replay-end') else ['target','capture']) if kind=='outcome' else [])
        validator=tmux['module_api']('runtime_client')['wire_contract']()['validate_schema']
        schema='error.lifecycle_outcome_unknown' if kind=='outcome' and operation in ('end','replay-end') else 'error.outcome_unknown' if kind=='outcome' else 'error.before_effect'
        validator(value,tmux['target_result_schema'](schema))
        print(json.dumps(value),flush=True)
        return tmux['TARGET_SPEC']['kind_exit'][kind]
