"""Closed legacy status/resolve surface. No arbitrary file, JSON or argv input.

Public registration/spawn admission remains closed at this checkpoint.
Internal fixed P/Q/proxy entries consume only an already registered owner.
Explicit finalization uses the fixed core parent/child owner-auth consumer;
private Python test seams are not CLI options or native evidence.
"""
import argparse
import json
import os
from pathlib import Path
import sys

# The configured proxy uses -I: Python then omits the script directory too.
# Resolve only this physical installed sibling library, never cwd/PYTHONPATH.
_LIBRARY=str(Path(__file__).resolve().parent)
if _LIBRARY not in sys.path:sys.path.insert(0,_LIBRARY)

from local_model_auth import LocalModelError
from runtime_legacy_registration import Store


class Parser(argparse.ArgumentParser):
    def error(self,message):raise LocalModelError('LOCAL_MODEL_PREVIEW_ARGUMENT_INVALID')


def _entry(args):
    from runtime_client import selected
    from runtime_intent_store import read_owner,checked_intent
    if selected()['mode']!='legacy':raise LocalModelError('LOCAL_MODEL_ADAPTER_UNSUPPORTED')
    root=Path(__file__).resolve().parents[2]
    store=Store(os.environ.get('AGENTSTACK_RUNTIME_DIR',str(root/'runtime')),args.name)
    intent=checked_intent(read_owner(store.owner)['launch_intent'])
    if intent['legacy_context']['install_root']!=str(root):raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
    supplied={key for key in ('intent_digest','registration_generation','launch_id','receipt_nonce')
        if getattr(args,key) is not None}
    expected={'runner':{'intent_digest','registration_generation'},
        'provider':{'launch_id','receipt_nonce'},'proxy':set(),
        'session-start':set(),'end-session':set(),'pre-edit':set(),'post-edit':set()}[args.kind]
    if supplied!=expected:raise LocalModelError('LOCAL_MODEL_PREVIEW_ARGUMENT_INVALID')
    if args.kind in ('session-start','end-session','pre-edit','post-edit'):
        from runtime_legacy_proxy import record_preview_session,guard_preview_edit
        edit=args.kind in ('pre-edit','post-edit')
        def pairs(items):
            value={}
            for key,item in items:
                if key in value:raise ValueError()
                value[key]=item
            return value
        def invalid(value):raise ValueError()
        try:
            raw=sys.stdin.read(1048577)
            if len(raw.encode('utf-8'))>1048576:raise ValueError()
            payload=json.loads(raw,object_pairs_hook=pairs,parse_constant=invalid)
        except (ValueError,OSError):
            raise LocalModelError('LOCAL_MODEL_RESERVATION_FAILED' if edit else 'LOCAL_MODEL_SESSION_FAILED') from None
        result=(guard_preview_edit if edit else record_preview_session)(intent,args.kind,payload)
        print(json.dumps(result,sort_keys=True));return 0
    if args.kind=='proxy':
        from runtime_legacy_proxy import configured_preview_proxy
        configured_preview_proxy(intent).serve_forever();return 0
    from runtime_legacy_worker import run_preview,execute_provider
    if args.kind=='provider':
        execute_provider(intent,launch_id=args.launch_id,receipt_nonce=args.receipt_nonce)
        raise LocalModelError('LOCAL_MODEL_EXECUTION_FAILED')
    row=run_preview(intent,intent_digest=args.intent_digest,registration_generation=args.registration_generation)
    code=row['execution']['outcome']['returncode']
    return code if code>=0 else 128-code


def main(argv=None,*,_store=None,_query_child=None,_observe_start=None,_stdout=None,_stderr=None):
    out=_stdout if _stdout is not None else sys.stdout
    err=_stderr if _stderr is not None else sys.stderr
    parser=Parser(description=__doc__)
    actions=parser.add_subparsers(dest='operation',required=True)
    status=actions.add_parser('status');status.add_argument('name')
    resolve=actions.add_parser('resolve');resolve.add_argument('name')
    resolve.add_argument('--expected-state-digest',required=True)
    resolve.add_argument('--action',required=True,choices=('finalize-registration','abandon'))
    entry=actions.add_parser('entry')
    entry.add_argument('kind',choices=('runner','provider','proxy','session-start','end-session','pre-edit','post-edit'));entry.add_argument('name')
    for key in ('intent-digest','registration-generation','launch-id','receipt-nonce'):
        entry.add_argument('--'+key)
    try:
        args=parser.parse_args(argv)
        if args.operation=='entry':
            if _store is not None:raise LocalModelError('LOCAL_MODEL_ADAPTER_UNSUPPORTED')
            return _entry(args)
        if _store is None:
            from runtime_client import selected
            if selected()['mode']!='legacy':raise LocalModelError('LOCAL_MODEL_ADAPTER_UNSUPPORTED')
            install=Path(__file__).resolve().parents[2]
            runtime=os.environ.get('AGENTSTACK_RUNTIME_DIR',str(install/'runtime'))
            store=Store(runtime,args.name)
            from runtime_intent_store import read_owner
            target=read_owner(store.owner).get('launch_intent',{}).get('legacy_context',{})
            if target.get('install_root')!=str(install):
                raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
            if args.operation=='resolve' and os.environ.get('AGENT_NAME')!=target.get('parent',{}).get('name'):
                raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
        else:
            store=_store
            if args.name!=store.name:raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
        if args.operation=='status':result=store.status()
        elif args.action=='finalize-registration':
            if _query_child is None:
                from runtime_intent_store import read_owner
                from runtime_legacy_mail import registration_finalizer
                _query_child=registration_finalizer(read_owner(store.owner)['launch_intent'])
            result=store.finalize(_query_child,expected_digest=args.expected_state_digest)
        else:
            if _observe_start is None:
                from process_observation import process_start
                _observe_start=process_start
            result=store.abandon(_observe_start,expected_digest=args.expected_state_digest)
        print(json.dumps(result,sort_keys=True),file=out);return 0
    except LocalModelError as exc:
        print(str(exc),file=err);return 2
    except (OSError,ValueError,TypeError,KeyError,RuntimeError):
        print('LOCAL_MODEL_CONFIGURATION_CHANGED',file=err);return 2


if __name__=='__main__':raise SystemExit(main())
