"""Pure explicit-preview metadata, never a native binding or launch approval.

The sole configured LaunchIntent owns this policy. This module validates data
and digest references only: it does not observe a CLI, apply configuration,
authenticate an owner, advance an execution phase, or bypass the native gate.
Legacy/global consumers must independently enforce their existing boundaries.
"""
import re

from local_model_auth import LocalModelError, canonical_digest
from runtime_intent_store import checked_intent

INVALID = 'LOCAL_MODEL_PREVIEW_POLICY_INVALID'
FIELDS = frozenset({'kind','native_scope','native_isolation','operator_decision'})


def validate_preview_policy(policy, *, provider, model_kind):
    """Configure's pure policy schema; this is not an operator/CLI proof."""
    if (type(policy) is not dict or set(policy) != FIELDS
            or any(type(item) is not str for item in policy.values())
            or policy['kind'] != 'local-preview-v1'
            or policy['native_scope'] != 'unverified'
            or policy['native_isolation'] != 'unverified'
            or re.fullmatch(r'[a-z0-9][a-z0-9._:-]{0,95}',policy['operator_decision']) is None):
        raise LocalModelError(INVALID)
    if provider != 'claude':
        raise LocalModelError('LOCAL_MODEL_ADAPTER_UNSUPPORTED')
    if model_kind != 'local-profile':
        raise LocalModelError(INVALID)
    return dict(policy)


def _policy(intent):
    """Return a fresh checked value; absent and explicitly null are different."""
    value = checked_intent(intent)
    if 'execution_policy' not in value:return None
    model=value.get('model')
    return validate_preview_policy(value['execution_policy'],provider=value.get('provider'),
        model_kind=model.get('kind') if isinstance(model,dict) else None)


def check_preview_request(intent, *, preview_requested):
    """Check a caller's explicit claim against stored policy; create nothing.

    A matching policy/request is NOT proof that an operator approved the owner,
    that source/configuration is unchanged, or that the CLI enforces a ceiling.
    """
    if type(preview_requested) is not bool:
        raise LocalModelError(INVALID)
    policy = _policy(intent)
    if policy is None:
        if preview_requested:
            raise LocalModelError('LOCAL_MODEL_PREVIEW_POLICY_REQUIRED')
        return None
    if not preview_requested:
        raise LocalModelError('LOCAL_MODEL_PREVIEW_CONFIRMATION_REQUIRED')
    return policy


def preview_notice(intent):
    """Closed nonsecret status data. Never emit effective configuration/auth."""
    policy = _policy(intent)
    if policy is None:
        return None
    return {'mode':'preview','native_scope_verified':False,'isolation_verified':False,
            'execution_policy_sha256':canonical_digest(policy)}


def check_preview_launch_reference(intent, row):
    """Compare ONLY a launch's policy reference, not its execution/authority.

    Callers must still validate the full transport-specific execution schema,
    binding, phase, nonce and real process evidence. A matching dict cannot
    authorize credential retrieval or native execution on its own.
    """
    expected = preview_notice(intent)
    if (type(row) is not dict or row.get('intent_digest') != intent['intent_digest']
            or (expected is None and 'preview' in row)
            or (expected is not None and (type(row.get('preview')) is not dict
                or canonical_digest(row['preview']) != canonical_digest(expected)))):
        raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
    return expected
