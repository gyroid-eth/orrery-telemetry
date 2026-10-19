"""Offline name admission for an already selected fixed client context.

Never resolve a name to another root or credential. RuntimeClient owns all
path, role, owner and binding validation; this is only the explicit target
argument check before construction that can create recovery/lock state.
"""
import re


def require_named_target(choice, name, api):
    error = api['ClientError']
    # Preserve the common entry's established missing-context reason before
    # interpreting a positional name. No target can be proved without a binding.
    if choice['mode'] == 'global' and choice.get('context') is None:
        raise error('CONTEXT_UNAVAILABLE')
    if not isinstance(name, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', name) is None:
        raise error('TARGET_UNAVAILABLE')
    if choice['mode'] != 'global' or choice.get('context') is None:
        raise error('CONTEXT_REQUIRED')
    client = api['RuntimeClient'](choice['context'], read_only=True)
    client._explicit_target_name = name
    client.check_explicit_target()
    return client
