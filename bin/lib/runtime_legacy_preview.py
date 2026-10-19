"""Legacy registration front-door refusal, before credentials/network effects.

At this checkpoint execution/configure/auth consumers are NOT connected.
Matching pure request metadata is never launch admission: local requests still
stop as unsupported after these checks. No HTTP, CLI, credentials or writes.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys

from local_model_auth import LocalModelError, secure_read
from runtime_intent_store import checked_intent, read_owner, check_idle, owner_layout
from runtime_preview_policy import check_preview_request


def registration_preconditions(runtime_dir,name,program,model,*,preview_requested=False):
    """Read only the fixed owner; do not confer operator/process authority."""
    if type(preview_requested) is not bool:
        raise LocalModelError('LOCAL_MODEL_PREVIEW_POLICY_INVALID')
    local=isinstance(model,str) and model.startswith('local:')
    if not name and not local and not preview_requested:return None
    # Keep legacy cloud's established literal name grammar. Only a new local
    # owner needs the narrower intent schema; do not preempt Mail's ordinary
    # identity handling just because this refusal checkpoint was added.
    pattern='[A-Za-z0-9][A-Za-z0-9_-]{0,127}' if local or preview_requested else '[A-Za-z0-9_.-]+'
    if not isinstance(name,str) or not re.fullmatch(pattern,name):
        raise LocalModelError('LOCAL_MODEL_INTENT_OWNER_REQUIRED')
    runtime=Path(runtime_dir)
    if not runtime.is_absolute() or '..' in runtime.parts:
        raise LocalModelError('LOCAL_MODEL_INTENT_OWNER_REQUIRED')
    owner=runtime/'child-agents'/(name+'.json')
    try:
        try:owner.lstat()
        except FileNotFoundError:frame={}
        else:
            if local or preview_requested:frame=read_owner(owner)
            else:
                # Ordinary private state may have a non-writable 0755 parent;
                # do not impose the NEW intent owner's exact 0700 on cloud.
                frame=json.loads(secure_read(owner,os.getuid(),mode=0o600))
                if not isinstance(frame,dict):raise ValueError()
        pending_paths=(owner_layout(owner,'legacy')[3] if local or preview_requested else (
            owner.with_name('.'+name+'.legacy-migration.json'),
            owner.with_name('.'+name+'.registration-pending.json')))
        for pending in pending_paths:
            try:pending.lstat()
            except FileNotFoundError:continue
            raise LocalModelError('LOCAL_MODEL_PENDING_CONFLICT')
        value=frame.get('launch_intent')
        if 'launch_intent' not in frame:
            if any(key in frame for key in ('local_execution','local_resolution','local_registration')):
                raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
            if preview_requested and not local:raise LocalModelError('LOCAL_MODEL_PREVIEW_POLICY_REQUIRED')
            if local:raise LocalModelError('LOCAL_MODEL_INTENT_OWNER_REQUIRED')
            return None
        value=checked_intent(value)
        if value.get('name')!=name:raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
        # Never let an ordinary cloud registration overwrite a configured owner.
        if not local:raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
        if program not in ('claude','claude-code'):raise LocalModelError('LOCAL_MODEL_ADAPTER_UNSUPPORTED')
        selected=value.get('model')
        if type(selected) is not dict or selected.get('ref')!=model:
            raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
        check_preview_request(value,preview_requested=preview_requested)
        check_idle(owner,'legacy')
        return {'owner':str(owner),'intent':value}
    except (OSError,ValueError,TypeError,KeyError):
        raise LocalModelError('LOCAL_MODEL_CONFIGURATION_CHANGED') from None


def admit_legacy_registration(runtime_dir,name,program,model,*,preview_requested=False):
    """Fail closed until real legacy resource/auth/exec admission is connected."""
    value=registration_preconditions(runtime_dir,name,program,model,preview_requested=preview_requested)
    if value is not None:raise LocalModelError('LOCAL_MODEL_ADAPTER_UNSUPPORTED')
    return None


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',required=True)
    parser.add_argument('--name',required=True)
    parser.add_argument('--program',required=True)
    parser.add_argument('--model',required=True)
    parser.add_argument('--preview',choices=('true','false'),required=True)
    args=parser.parse_args(argv)
    try:
        admit_legacy_registration(args.runtime,args.name,args.program,args.model,preview_requested=args.preview=='true')
        return 0
    except LocalModelError as exc:
        print(str(exc),file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
