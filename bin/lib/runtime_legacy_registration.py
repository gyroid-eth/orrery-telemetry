"""Private legacy local registration lifecycle, not a native admission entry.

Same owner/pending/token slots, no second registry. Transport and process
observations are supplied by fixed consumers, not by caller JSON proofs.
The standalone front door remains unsupported until those consumers and the
full resource/auth/exec boundary are connected. No transport is run on import.
"""
from copy import deepcopy
import os
from pathlib import Path
import re
import secrets

from local_model_auth import LocalModelError, canonical_digest, plain_text, secure_read
from runtime_intent_store import checked_intent, owner_layout, owner_lock, read_owner, write_owner, write_private_bytes
from runtime_preview_policy import check_preview_request, preview_notice
from runtime_legacy_context import checked_legacy_context

KIND='orrery-legacy-preview-registration-v1'
CHANGED='LOCAL_MODEL_STATE_CHANGED'
CONFLICT='LOCAL_MODEL_PENDING_CONFLICT'
IDENTITY='LOCAL_MODEL_IDENTITY_MISMATCH'
AUTH='LOCAL_MODEL_AUTH_FAILED'
PENDING_FIELDS={'kind','generation','intent_digest','phase','request','credential_ref','response'}
IDENTITY_FIELDS={'id','name','program','model','project_id'}


def _present(path):
    try:path.lstat();return True
    except FileNotFoundError:return False


def _credential(value):
    return isinstance(value,str) and re.fullmatch('[A-Za-z0-9_-]{24,512}',value) is not None


def _hex(value,size):
    return isinstance(value,str) and re.fullmatch('[0-9a-f]{'+str(size)+'}',value) is not None


def _identity(value,*,credential=False):
    fields=IDENTITY_FIELDS|({'registration_token'} if credential else set())
    return (type(value) is dict and set(value)==fields
        and type(value['id']) is int and value['id']>0
        and type(value['project_id']) is int and value['project_id']>0
        and all(plain_text(value[key]) and len(value[key])<=512 for key in ('name','program','model'))
        and (not credential or _credential(value['registration_token'])))


class Store:
    """Internal fixed-layout operations. Metadata alone never admits launch."""
    def __init__(self,runtime,name):
        self.runtime=Path(runtime)
        self.owner=self.runtime/'child-agents'/(name+'.json')
        _,_,_,pending=owner_layout(self.owner,'legacy')
        self.name=name;self.pending=pending[1]
        self.token=self.runtime/('agent_token_'+name)

    def _request(self,frame):
        value=checked_intent(frame.get('launch_intent'))
        check_preview_request(value,preview_requested=True)
        try:
            context=checked_legacy_context(value['legacy_context']);wire=value['model']['resolved']['model']
            if (value['name']!=self.name or context['name']!=self.name
                    or context['runtime_dir']!=str(self.runtime) or context['owner']!=str(self.owner)
                    or type(context['project_id']) is not int or context['project_id']<=0
                    or not plain_text(context['project_key']) or not plain_text(wire)):
                raise ValueError()
            return value,{'name':self.name,'project_key':context['project_key'],
                'program':'claude-code','model':wire,'task_description':'Delegated local preview'}
        except (KeyError,TypeError,ValueError):raise LocalModelError(IDENTITY) from None

    def _snapshot(self):
        try:
            frame=read_owner(self.owner);intent,request=self._request(frame)
            pending=read_owner(self.pending) if _present(self.pending) else None
            token=secure_read(self.token,os.getuid(),mode=0o600,parent_mode=0o700,limit=4096) if _present(self.token) else None
            if pending is not None:
                if (set(pending)!=PENDING_FIELDS or pending['kind']!=KIND
                        or not isinstance(pending['generation'],str)
                        or not re.fullmatch('[0-9a-f]{32}',pending['generation'])
                        or pending['intent_digest']!=intent['intent_digest'] or pending['request']!=request
                        or pending['credential_ref']!=str(self.token)
                        or pending['phase'] not in ('prepared','sent-unknown','response-saved','response-rejected','finalizing')
                        or (pending['phase'] in ('prepared','sent-unknown') and pending['response'] is not None)
                        or (pending['phase'] in ('response-saved','response-rejected','finalizing')
                            and not _identity(pending['response'],credential=True))):
                    raise LocalModelError(CONFLICT)
            registration=frame.get('local_registration');resolution=frame.get('local_resolution')
            if 'local_registration' in frame:
                if (type(registration) is not dict or set(registration)!={'phase','generation','intent_digest','remote_effect'}
                        or registration['phase']!='registered' or registration['remote_effect']!='confirmed'
                        or registration['intent_digest']!=intent['intent_digest'] or not _hex(registration['generation'],32)
                        or type(frame.get('agent_id')) is not int or frame['agent_id']<=0
                        or frame.get('agent_name')!=self.name or not _credential(frame.get('registration_token'))
                        or frame['registration_token'].encode()!=token):
                    raise LocalModelError(CONFLICT)
            if 'local_resolution' in frame:
                if (type(resolution) is not dict or set(resolution)!={'phase','previous_state_digest','remote_effect','generation','launch_id'}
                        or resolution['phase'] not in ('cancelled','abandoned')
                        or resolution['remote_effect'] not in ('confirmed','unknown','not-sent')
                        or not _hex(resolution['previous_state_digest'],64)
                        or (resolution['generation'] is not None and not _hex(resolution['generation'],32))
                        or (resolution['launch_id'] is not None and not _hex(resolution['launch_id'],32))):
                    raise LocalModelError(CONFLICT)
            if 'local_execution' in frame:
                from runtime_legacy_execution import checked_execution
                checked_execution(frame['local_execution'],intent)
            # No status returns this private snapshot or any credential bytes.
            value={'owner':frame,'pending':pending,'credential_bytes':token.hex() if token is not None else None}
            if read_owner(self.owner)!=frame or (read_owner(self.pending) if _present(self.pending) else None)!=pending:
                raise LocalModelError(CHANGED)
            return value,canonical_digest(value)
        except (OSError,ValueError,TypeError,KeyError):
            raise LocalModelError('LOCAL_MODEL_CONFIGURATION_CHANGED') from None

    def _expect(self,digest):
        value,current=self._snapshot()
        if not isinstance(digest,str) or digest!=current:raise LocalModelError(CHANGED)
        return value

    @staticmethod
    def _unresolved(value):
        if 'local_resolution' in value['owner']:raise LocalModelError(CONFLICT)

    def status(self):
        """Read-only, nonsecret projection; no repair, auth or lock creation."""
        value,digest=self._snapshot();frame=value['owner'];pending=value['pending']
        resolution=frame.get('local_resolution');execution=frame.get('local_execution')
        registration=frame.get('local_registration')
        phase=(resolution['phase'] if resolution else execution['phase'] if execution else
            pending['phase'] if pending else registration['phase'] if registration else 'configured')
        remote=(resolution['remote_effect'] if resolution else 'confirmed' if registration else
            'not-sent' if pending is None or pending['phase']=='prepared' else 'unknown')
        return {'name':self.name,'intent_digest':frame['launch_intent']['intent_digest'],
            'preview':preview_notice(frame['launch_intent']),
            'state_digest':digest,'phase':phase,'remote_effect':remote,
            'generation':pending['generation'] if pending else registration.get('generation') if registration else None,
            'launch_id':execution.get('launch_id') if execution else None,
            'execution_outcome':'unknown' if resolution and execution else
                execution['execution']['outcome']['state'] if execution else 'not-started',
            'remote_cleanup':'unknown' if resolution and execution else
                execution['termination']['phase'] if execution else 'not-started'}

    def begin(self,*,expected_digest,candidate_token,checkpoint=None):
        """Save request before HTTP; caller must already pass full admission."""
        if not _credential(candidate_token):raise LocalModelError(AUTH)
        with owner_lock(self.owner,'legacy'):
            value=self._expect(expected_digest);self._unresolved(value)
            if (value['pending'] is not None or value['credential_bytes'] is not None
                    or any(key in value['owner'] for key in ('agent_id','local_execution','local_registration'))):
                raise LocalModelError(CONFLICT)
            intent,request=self._request(value['owner'])
            pending={'kind':KIND,'generation':secrets.token_hex(16),'intent_digest':intent['intent_digest'],
                'phase':'prepared','request':request,'credential_ref':str(self.token),'response':None}
            write_owner(self.pending,pending)
            if checkpoint:checkpoint('registration-prepared')
            write_private_bytes(self.token,candidate_token.encode())
        return self.status()

    def send_once(self,register,*,expected_digest,checkpoint=None):
        with owner_lock(self.owner,'legacy'):
            value=self._expect(expected_digest);self._unresolved(value);pending=value['pending']
            if pending is None or pending['phase']!='prepared':raise LocalModelError(CONFLICT)
            token=bytes.fromhex(value['credential_bytes'] or '').decode()
            if not _credential(token):raise LocalModelError(AUTH)
            pending=deepcopy(pending);pending['phase']='sent-unknown';write_owner(self.pending,pending)
            sent_digest=self.status()['state_digest']
        try:response=register(deepcopy(pending['request']),token)
        except Exception:raise LocalModelError('LOCAL_MODEL_REGISTRATION_UNKNOWN') from None
        # A return is first saved, never silently re-register after interruption.
        if not _identity(response,credential=True):
            raise LocalModelError('LOCAL_MODEL_REGISTRATION_UNKNOWN')
        matched=self._matches(response,value['owner']['launch_intent'])
        with owner_lock(self.owner,'legacy'):
            self._expect(sent_digest)
            pending['response']=deepcopy(response)
            pending['phase']='response-saved' if matched else 'response-rejected'
            write_owner(self.pending,pending)
            if checkpoint:checkpoint('response-saved')
        if not matched:raise LocalModelError(IDENTITY)
        return self.status()

    def _matches(self,response,intent):
        _,request=self._request({'launch_intent':intent})
        return (type(response) is dict and type(response.get('id')) is int and response['id']>0
            and type(response.get('project_id')) is int and response['project_id']==intent['legacy_context']['project_id']
            and all(response.get(key)==request[key] for key in ('name','program','model')))

    def finalize(self,query_child,*,expected_digest,checkpoint=None):
        """Explicit owner-authenticated finalization, never register or exec."""
        with owner_lock(self.owner,'legacy'):
            value=self._expect(expected_digest);self._unresolved(value);pending=value['pending']
            if pending is None or pending['phase'] not in ('sent-unknown','response-saved','finalizing'):
                raise LocalModelError(CONFLICT)
            response=pending['response'];intent=value['owner']['launch_intent']
            token=response['registration_token'] if response is not None else bytes.fromhex(value['credential_bytes'] or '').decode()
            if not _credential(token):raise LocalModelError(AUTH)
        try:identity=query_child(token)
        except Exception:raise LocalModelError(AUTH) from None
        if (not _identity(identity) or not self._matches(identity,intent)
                or (response is not None and identity!={k:v for k,v in response.items() if k!='registration_token'})):
            raise LocalModelError(IDENTITY)
        with owner_lock(self.owner,'legacy'):
            self._expect(expected_digest)
            if value['owner'].get('agent_id',identity['id'])!=identity['id']:raise LocalModelError(IDENTITY)
            pending=deepcopy(pending);pending['phase']='finalizing'
            pending['response']={**identity,'registration_token':token}
            write_owner(self.pending,pending)
            write_private_bytes(self.token,token.encode())
            if checkpoint:checkpoint('canonical-token-published')
            frame=value['owner']
            frame={**frame,'agent_id':identity['id'],'agent_name':self.name,'program':'claude-code',
                'project_key':pending['request']['project_key'],'registration_token':token,
                'local_registration':{'phase':'registered','generation':pending['generation'],
                    'intent_digest':intent['intent_digest'],'remote_effect':'confirmed'}}
            write_owner(self.owner,frame)
            if checkpoint:checkpoint('owner-published')
            self.pending.unlink()
            directory=os.open(self.pending.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(directory)
            finally:os.close(directory)
        return self.status()

    def abandon(self,observe_start,*,expected_digest):
        """Absence of recorded actors only; preserve unknown remote effects."""
        with owner_lock(self.owner,'legacy'):
            value=self._expect(expected_digest);self._unresolved(value)
            execution=value['owner'].get('local_execution')
        if execution is not None:
            from runtime_legacy_execution import checked_execution
            execution=checked_execution(execution,value['owner']['launch_intent'])
            for role in ('runner','provider'):
                process=execution['execution'][role]
                if process is None:continue
                try:birth=observe_start(process['pid'])
                except Exception:raise LocalModelError('LOCAL_MODEL_OBSERVATION_INCOMPLETE') from None
                if not plain_text(birth):raise LocalModelError('LOCAL_MODEL_OBSERVATION_INCOMPLETE')
                if birth==process['process_start']:raise LocalModelError('LOCAL_MODEL_PROCESS_STILL_RUNNING')
                # absent OR a different birth: never signal the reused PID.
        pending=value['pending'];sent=bool(pending and pending['phase']!='prepared')
        phase='abandoned' if execution is not None or sent else 'cancelled'
        with owner_lock(self.owner,'legacy'):
            self._expect(expected_digest)
            frame=deepcopy(value['owner']);frame['local_resolution']={'phase':phase,
                'previous_state_digest':expected_digest,'remote_effect':'unknown' if sent else
                    'confirmed' if frame.get('local_registration') else 'not-sent',
                'generation':pending['generation'] if pending else None,
                'launch_id':execution['launch_id'] if execution else None}
            write_owner(self.owner,frame)
        return self.status()
