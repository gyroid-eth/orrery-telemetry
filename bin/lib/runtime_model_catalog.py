"""Catalog intent delegates to the existing provider policy, never a new table.

Resolution belongs to an explicit caller plan. Resume validates the saved formal
ID/effort; it does not resolve the alias or choose today's catalog default again.
Old global profiles with no intent retain R2's native-default path.
"""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

from local_model_auth import LocalModelError, intent_digest, secure_read
from runtime_caller_schema import CALLER_FIELDS, OPTIONAL_FIELDS, canonical_caller

INVALID = 'MODEL_SELECTION_INVALID'


def provider_policy(provider):
    if provider not in ('codex','claude'):raise LocalModelError(INVALID)
    path = Path(__file__).resolve().parents[2]/'dashboard'/(provider+'_models.py')
    existing = sys.modules.get('dashboard.'+provider+'_models')
    if existing is not None and Path(existing.__file__).resolve() == path:
        return existing
    name = '_orrery_catalog_'+provider+'_'+hashlib.sha256(str(path).encode()).hexdigest()
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name,path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name,None)
            raise
    return sys.modules[name]


def _claude_model(policy, raw):
    # The shell and this consumer share the same existing policy normalizer.
    # Do not duplicate its alias table or add a second fallback policy here.
    value = policy.normalize_model(raw)
    if not policy.is_model_id(value):raise ValueError()
    catalog = policy.resolve_catalog(())
    if catalog.error or (catalog.source=='override' and value not in catalog.models):raise ValueError()
    return value


def resolve_catalog_intent(caller, context):
    caller = canonical_caller(caller)
    if caller.get('strict_output_cap',False):raise LocalModelError('MODEL_OUTPUT_CAP_UNSUPPORTED')
    if context.get('identity',{}).get('name',caller['name']) != caller['name']:
        raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
    try:
        policy = provider_policy(caller['provider'])
        if caller['provider']=='codex':
            model = policy.normalize_model(caller['model'])
            effort = policy.resolve_effort(model,caller.get('effort') or '')
        else:
            if caller.get('effort') not in (None,''):raise ValueError()
            model,effort = _claude_model(policy,caller['model']),None
        value = {**deepcopy(caller),'schema_version':1,'revision':1,
                 'operator_uid':context['operator_uid'],
                 'model':{'kind':'catalog','requested':caller['model'],'id':model,'effort':effort}}
        task = Path(caller['task'])
        value['task_sha256'] = hashlib.sha256(secure_read(task,context['operator_uid'])).hexdigest() if task.exists() or task.is_symlink() else None
        value['intent_digest'] = intent_digest(value)
        return value
    except (KeyError,TypeError,ValueError,OSError,ImportError,AttributeError):raise LocalModelError(INVALID) from None


def materialize_catalog_intent(intent, context, *, provider_session_id=None):
    """Validate explicit formal selection, without alias/default re-resolution."""
    try:
        if intent['intent_digest'] != intent_digest(intent) or intent['operator_uid'] != context['operator_uid']:
            raise ValueError()
        model = intent['model']
        if set(model) != {'kind','requested','id','effort'} or model['kind']!='catalog':raise ValueError()
        caller = {key:value for key,value in intent.items() if key in CALLER_FIELDS|OPTIONAL_FIELDS}
        caller['model'] = model['requested']
        canonical_caller(caller)
        if caller['model'].startswith('local:'):raise ValueError()
        if caller.get('strict_output_cap',False):raise ValueError()
        policy = provider_policy(intent['provider'])
        if not policy.is_model_id(model['id']):raise ValueError()
        argv = ['--model='+model['id']]
        if intent['provider']=='codex':
            if policy.normalize_model(model['id']) != model['id']:raise ValueError()
            effort = model['effort']
            if not isinstance(effort,str):raise ValueError()
            if effort:
                if policy.resolve_effort(model['id'],effort) != effort:raise ValueError()
                argv += ['-c','model_reasoning_effort='+json.dumps(effort)]
        else:
            if model['effort'] is not None or _claude_model(policy,model['id']) != model['id']:
                raise ValueError()
        return {'model':model['id'],'execution':{'argv':argv,'env':{},'cwd':intent['cwd']},
                'inherit_env':True,'warm_pool':True,'intent_digest':intent['intent_digest'],
                'provider_session_id':provider_session_id}
    except (KeyError,TypeError,ValueError,OSError,ImportError,AttributeError):raise LocalModelError(INVALID) from None
