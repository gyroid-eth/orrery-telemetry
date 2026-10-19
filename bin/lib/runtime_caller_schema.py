"""One private, provider-independent caller shape; never operator context.

Adapters may rename their flags, but may not accept model roots, authentication,
identity, transport or permissions evidence through this schema. Optional keys
remain absent: omission, an explicit false, and an empty list are not aliases.
Existing tools/model policy owns interpretation after this structural check.
"""
from copy import deepcopy
from pathlib import Path
import re

from local_model_auth import LocalModelError, plain_text

CALLER_FIELDS = frozenset({"provider", "model", "cwd", "task", "name", "parent",
                          "tools", "base", "clear_tools", "detached", "resources",
                          "worktree", "chrome"})
OPTIONAL_FIELDS = frozenset({"effort", "strict_output_cap", "task_delivery"})
PROVIDERS = {"codex": "codex", "codex-cli": "codex",
             "claude": "claude", "claude-code": "claude"}
INVALID = "LOCAL_MODEL_CALLER_INVALID"


def canonical_caller(caller):
    """Validate literal input without reading files, resolving aliases or effects."""
    try:
        if (not isinstance(caller, dict) or not CALLER_FIELDS <= caller.keys()
                or caller.keys() - CALLER_FIELDS - OPTIONAL_FIELDS):
            raise ValueError()
        result = deepcopy(caller)
        result['provider'] = PROVIDERS[result['provider']]
        for key in ('model', 'name', 'cwd', 'task'):
            if not plain_text(result[key]):
                raise ValueError()
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', result['name']):
            raise ValueError()
        if not isinstance(result['parent'], str) or (result['parent'] and not plain_text(result['parent'])):
            raise ValueError()
        for key in ('cwd', 'task'):
            if not Path(result[key]).is_absolute() or '..' in Path(result[key]).parts:
                raise ValueError()
        for key in ('clear_tools', 'detached', 'worktree', 'chrome'):
            if type(result[key]) is not bool:
                raise ValueError()
        if result['base'] not in ('default', 'mail-only'):
            raise ValueError()
        if not isinstance(result['tools'], (list, dict)):
            raise ValueError()
        if isinstance(result['tools'], list) and not all(plain_text(item) for item in result['tools']):
            raise ValueError()
        if not isinstance(result['resources'], list) or not all(plain_text(item) for item in result['resources']):
            raise ValueError()
        if 'effort' in result and result['effort'] is not None and not isinstance(result['effort'], str):
            raise ValueError()
        if 'strict_output_cap' in result and type(result['strict_output_cap']) is not bool:
            raise ValueError()
        if 'task_delivery' in result and result['task_delivery'] not in ('mail', 'embedded'):
            raise ValueError()
        return result
    except (KeyError, TypeError, ValueError):
        raise LocalModelError(INVALID) from None
