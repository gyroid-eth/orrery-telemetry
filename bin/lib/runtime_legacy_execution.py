"""Closed legacy execution facts in the existing child owner, not authority.

Readers/builders are data-only. ExecutionStore is an internal current-process
producer under the same name transaction, NOT resource/native admission. It
does not spawn a provider, authenticate a session, or resolve unknown effects.
The outer fixed consumers must enforce admission and own the actual child wait.
A plausible dict or a unit's fake OS observation is never native launch proof.
"""
from copy import deepcopy
import os
import re

from local_model_auth import LocalModelError, plain_text, canonical_digest
from runtime_intent_store import checked_intent
from runtime_preview_policy import check_preview_request, check_preview_launch_reference, preview_notice

INVALID='LOCAL_MODEL_EXECUTION_INVALID'
FIELDS=frozenset({'schema_version','launch_id','receipt_nonce','intent_digest','intent_revision',
    'phase','execution','session_receipt','termination','preview','registration_guarantee'})


def _object(value,fields):
    return type(value) is dict and set(value)==set(fields)


def _process(value):
    return (value is None or (_object(value,{'pid','process_start'}) and type(value['pid']) is int
        and value['pid']>0 and plain_text(value['process_start']) and value['process_start']!='absent'))


def checked_execution(row,intent):
    """Return fresh data after structural/intent checks, NOT process checks."""
    try:
        intent=checked_intent(intent)
        check_preview_request(intent,preview_requested=True)
        if (not _object(row,FIELDS) or type(row['schema_version']) is not int or row['schema_version']!=1
                or type(row['intent_revision']) is not int or row['intent_revision']!=intent['revision']
                or not isinstance(row['launch_id'],str) or not re.fullmatch('[0-9a-f]{32}',row['launch_id'])
                or not isinstance(row['receipt_nonce'],str)
                or not re.fullmatch('[A-Za-z0-9_-]{32,128}',row['receipt_nonce'])):
            raise ValueError()
        check_preview_launch_reference(intent,row)
        guarantee=row['registration_guarantee']
        if (not _object(guarantee,{'level','atomic_create_only'})
                or guarantee['level']!='ordinary-child' or guarantee['atomic_create_only'] is not False):
            raise ValueError()
        phase=row['phase'];execution=row['execution'];termination=row['termination'];session=row['session_receipt']
        if (phase not in ('planned','gated','exec-unknown','running','ended','cancelled')
                or not _object(execution,{'runner','provider','outcome'})
                or not _process(execution['runner']) or not _process(execution['provider'])
                or not _object(execution['outcome'],{'state','returncode'})
                or not _object(termination,{'phase','lease_release','retire'})
                or termination['phase'] not in ('pending','unknown','ended')
                or any(termination[key] not in ('pending','unknown','complete') for key in ('lease_release','retire'))):
            raise ValueError()
        runner,provider=execution['runner'],execution['provider']
        outcome=execution['outcome'];state=outcome['state'];code=outcome['returncode']
        if ((runner and provider and runner['pid']==provider['pid'])
                or (phase in ('gated','exec-unknown','running','ended') and runner is None)
                or (phase in ('planned','gated','cancelled') and provider is not None)
                or (phase in ('running','ended') and provider is None)):
            raise ValueError()
        if session is not None and (not _object(session,{'session_id','phase'})
                or not plain_text(session['session_id']) or session['phase'] not in ('started','ended')
                or provider is None):
            raise ValueError()
        if phase=='running' and session is None:raise ValueError()
        if state not in ('pending','unknown','ended'):raise ValueError()
        if state in ('pending','unknown') and code is not None:raise ValueError()
        if state=='unknown' and phase!='exec-unknown':raise ValueError()
        if (phase in ('ended','cancelled')) != (state=='ended'):raise ValueError()
        if phase=='ended' and type(code) is not int:raise ValueError()
        if phase=='cancelled' and (code is not None or session is not None):raise ValueError()
        if termination['phase']=='ended' and (phase not in ('ended','cancelled')
                or any(termination[key]!='complete' for key in ('lease_release','retire'))
                or (phase=='ended' and (session is None or session['phase']!='ended'))):
            raise ValueError()
        return deepcopy(row)
    except (KeyError,TypeError,ValueError):
        raise LocalModelError(INVALID) from None


def new_execution(intent,*,launch_id,receipt_nonce):
    """Build a pending record. Persistence/real identity still belong to exec."""
    value={'schema_version':1,'launch_id':launch_id,'receipt_nonce':receipt_nonce,
        'intent_digest':intent['intent_digest'],'intent_revision':intent['revision'],'phase':'planned',
        'execution':{'runner':None,'provider':None,'outcome':{'state':'pending','returncode':None}},
        'session_receipt':None,'termination':{'phase':'pending','lease_release':'pending','retire':'pending'},
        'preview':preview_notice(intent),
        'registration_guarantee':{'level':'ordinary-child','atomic_create_only':False}}
    return checked_execution(value,intent)


def execution_is_idle(row,intent):
    value=checked_execution(row,intent)
    return value['phase'] in ('ended','cancelled') and value['termination']['phase']=='ended'


def active_execution_snapshot(owner,intent):
    """Private same-owner facts for auth/proxy; NOT process/launch admission.

    Callers must first validate pinned configuration/source and then prove the
    actual helper/Q/P processes, with a second snapshot afterwards. Credentials
    in the returned private owner must never enter tool/status/log output.
    This reads the existing Store only; no second identity table or cache.
    """
    from pathlib import Path
    from runtime_legacy_context import checked_legacy_context
    from runtime_legacy_registration import Store
    from runtime_legacy_configuration import check_legacy_provider_outputs
    failure='LOCAL_MODEL_AUTH_FAILED'
    try:
        context=checked_legacy_context(intent['legacy_context'])
        if Path(owner)!=Path(context['owner']):raise LocalModelError(failure)
        store=Store(context['runtime_dir'],context['name'])
        saved,_=store._snapshot()
        frame=saved['owner']
        if (saved['pending'] is not None or 'local_registration' not in frame
                or 'local_resolution' in frame or frame['launch_intent']!=intent):
            raise LocalModelError(failure)
        row=checked_execution(frame['local_execution'],intent)
        provider=row['execution']['provider']
        if (row['phase'] not in ('exec-unknown','running') or provider is None
                or row['execution']['outcome']!={'state':'pending','returncode':None}
                or intent['operator_uid']!=os.getuid()
                or os.environ.get('AGENTSTACK_LAUNCH_ID')!=row['launch_id']
                or os.environ.get('AGENTSTACK_LAUNCH_NONCE')!=row['receipt_nonce']
                or os.environ.get('AGENTSTACK_PROVIDER_PID')!=str(provider['pid'])):
            raise LocalModelError(failure)
        check_legacy_provider_outputs(intent)
        return {'owner':frame,'row':row}
    except (KeyError,TypeError,OSError,ValueError):raise LocalModelError(failure) from None


class _CurrentProcess:
    """Existing shared observation, no new ps parser or caller PID input."""
    def pid(self):return os.getpid()
    def parent(self):return os.getppid()
    def start(self,pid):
        from process_observation import process_start
        return process_start(pid)


class ExecutionStore:
    """Internal P/Q state producer. No public argv may select _process.

    plan is called only after the outer resource/preview/registration gate.
    P and Q subsequently publish their OWN observed identities. All OS queries
    and the actual wait stay outside the name lock. Unknown is never cancelled
    or turned into a successful exit by these transitions.
    """
    def __init__(self,runtime,name,*,_process=None):
        from runtime_legacy_registration import Store
        self.store=Store(runtime,name)
        self.process=_process if _process is not None else _CurrentProcess()

    def _registered(self):
        value,digest=self.store._snapshot();self.store._unresolved(value)
        if value['pending'] is not None or 'local_registration' not in value['owner']:
            raise LocalModelError('LOCAL_MODEL_PENDING_CONFLICT')
        return value['owner'],digest

    def _row(self,launch_id,receipt_nonce):
        frame,digest=self._registered()
        row=checked_execution(frame.get('local_execution'),frame['launch_intent'])
        if row['launch_id']!=launch_id or row['receipt_nonce']!=receipt_nonce:
            raise LocalModelError('LOCAL_MODEL_STATE_CHANGED')
        return frame,row,digest

    def _birth(self,pid):
        if type(pid) is not int or pid<=0:raise LocalModelError('LOCAL_MODEL_OBSERVATION_INCOMPLETE')
        try:value=self.process.start(pid)
        except Exception:raise LocalModelError('LOCAL_MODEL_OBSERVATION_INCOMPLETE') from None
        if not plain_text(value) or value=='absent':raise LocalModelError('LOCAL_MODEL_OBSERVATION_INCOMPLETE')
        return value

    def _runner(self,row):
        expected=row['execution']['runner']
        pid=self.process.pid()
        if (expected is None or type(pid) is not int or pid!=expected['pid']
                or self._birth(expected['pid'])!=expected['process_start']):
            raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')

    def _save(self,row,digest):
        from runtime_intent_store import owner_lock,write_owner
        with owner_lock(self.store.owner,'legacy'):
            value=self.store._expect(digest);self.store._unresolved(value)
            frame=value['owner'];row=checked_execution(row,frame['launch_intent'])
            write_owner(self.store.owner,{**frame,'local_execution':row})
        return deepcopy(row)

    def plan(self,*,expected_state_digest,intent_digest,registration_generation):
        from runtime_intent_store import owner_lock,write_owner
        import secrets
        with owner_lock(self.store.owner,'legacy'):
            value=self.store._expect(expected_state_digest);self.store._unresolved(value);frame=value['owner']
            if (value['pending'] is not None or 'local_registration' not in frame or 'local_execution' in frame
                    or frame.get('local_registration',{}).get('generation')!=registration_generation
                    or frame['launch_intent']['intent_digest']!=intent_digest):
                raise LocalModelError('LOCAL_MODEL_PENDING_CONFLICT')
            row=new_execution(frame['launch_intent'],launch_id=secrets.token_hex(16),receipt_nonce=secrets.token_urlsafe(32))
            write_owner(self.store.owner,{**frame,'local_execution':row})
        return deepcopy(row)

    def claim_runner(self,*,launch_id,receipt_nonce):
        _,row,digest=self._row(launch_id,receipt_nonce)
        if row['phase']!='planned':raise LocalModelError('LOCAL_MODEL_PENDING_CONFLICT')
        pid=self.process.pid();birth=self._birth(pid)
        if self.process.pid()!=pid or self._birth(pid)!=birth:
            raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
        row['execution']['runner']={'pid':pid,'process_start':birth};row['phase']='gated'
        return self._save(row,digest)

    def before_provider(self,*,launch_id,receipt_nonce):
        _,row,digest=self._row(launch_id,receipt_nonce)
        if row['phase']!='gated':raise LocalModelError('LOCAL_MODEL_PENDING_CONFLICT')
        self._runner(row)
        # Persist uncertainty BEFORE Popen/fork, never after its return.
        row['phase']='exec-unknown';row['execution']['outcome']={'state':'unknown','returncode':None}
        return self._save(row,digest)

    def claim_provider(self,*,launch_id,receipt_nonce):
        _,row,digest=self._row(launch_id,receipt_nonce)
        if (row['phase']!='exec-unknown' or row['execution']['provider'] is not None
                or row['execution']['outcome']!={'state':'unknown','returncode':None}):
            raise LocalModelError('LOCAL_MODEL_PENDING_CONFLICT')
        runner=row['execution']['runner'];pid=self.process.pid()
        if type(pid) is not int or pid<=0 or pid==runner['pid'] or self.process.parent()!=runner['pid']:
            raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
        if self._birth(runner['pid'])!=runner['process_start']:
            raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
        birth=self._birth(pid)
        if (self.process.pid()!=pid or self.process.parent()!=runner['pid']
                or self._birth(runner['pid'])!=runner['process_start'] or self._birth(pid)!=birth):
            raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
        row['execution']['provider']={'pid':pid,'process_start':birth}
        row['execution']['outcome']={'state':'pending','returncode':None}
        return self._save(row,digest)

    def finish_wait(self,child,*,launch_id,receipt_nonce):
        """The fixed P consumer supplies its actual Popen object, not JSON rc.

        Q/SessionStart/End may legitimately advance while P waits. Revalidate
        those fresh facts, but reject identity/credential/intent changes. This
        records wait only, never remote lease/retire success or cleanup complete.
        """
        before,row,_=self._row(launch_id,receipt_nonce);self._runner(row)
        provider=row['execution']['provider']
        if (row['phase'] not in ('exec-unknown','running') or type(child.pid) is not int
                or child.pid<=0 or child.pid==row['execution']['runner']['pid']
                or (provider is not None and child.pid!=provider['pid'])):
            raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
        code=child.wait()  # no lock while the provider runs
        if type(code) is not int:raise LocalModelError('LOCAL_MODEL_OBSERVATION_INCOMPLETE')
        frame,current,digest=self._row(launch_id,receipt_nonce)
        stable=lambda value:canonical_digest({key:item for key,item in value.items() if key!='local_execution'})
        if (stable(before)!=stable(frame) or current['phase'] not in ('exec-unknown','running')
                or current['execution']['runner']!=row['execution']['runner']
                or current['execution']['provider'] is None or current['execution']['provider']['pid']!=child.pid
                or (provider is not None and current['execution']['provider']!=provider)
                or current['execution']['outcome']!={'state':'pending','returncode':None}):
            raise LocalModelError('LOCAL_MODEL_STATE_CHANGED')
        self._runner(current)
        try:birth=self.process.start(child.pid)
        except Exception:raise LocalModelError('LOCAL_MODEL_OBSERVATION_INCOMPLETE') from None
        if not plain_text(birth) or birth==current['execution']['provider']['process_start']:
            raise LocalModelError('LOCAL_MODEL_OBSERVATION_INCOMPLETE')
        # 'absent' OR a different live birth means this exact Q no longer
        # exists. Never signal or adopt a reused PID.
        self._runner(current)
        current['phase']='ended';current['execution']['outcome']={'state':'ended','returncode':code}
        return self._save(current,digest)
