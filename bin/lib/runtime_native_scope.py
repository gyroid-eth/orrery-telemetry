"""Strict native configuration proposal, NOT a permission execution engine.

There is no adopted/version-pinned native capability contract yet. A proposal
therefore carries explicit unresolved built-in/source-closure evidence and is
never launch admission. In particular, a list of extra allows is not a ceiling
on the CLI's built-in operations. Only a later independently verified native
binding can close those gates; neither an operator string nor a binary hash
does that. No native CLI, shell parser or permission simulator runs here.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import tomllib

from local_model_auth import LocalModelError, canonical_digest, secure_read, source_evidence

UNSUPPORTED = 'LOCAL_MODEL_ADAPTER_UNSUPPORTED'
INVALID = 'LOCAL_MODEL_PERMISSION_PLAN_UNSATISFIABLE'
LAYERS = {'system','user','project','project-local','managed','cloud'}
SCOPE_FIELDS = {'read','edit','bash','built_in_read','mail_tools','sandbox_auto_allow',
                'inherited_bash_allow','reservation_hook','settings_sources','cli_version'}


def _json_object(pairs):
    value = {}
    for key,item in pairs:
        if key in value:raise ValueError()
        value[key] = item
    return value


def _read_sources(provider, sources):
    """Pin explicit sources, refusing settings outside this small known subset.

    The provider configure caller owns source enumeration. These records do not
    assert that MDM/cloud/project discovery was exhaustive or that layer order
    for an unpinned CLI is known. Reject nonempty inherited permission rules
    rather than inventing wildcard/deny/ask precedence or a Bash interpreter.
    """
    if not isinstance(sources,list) or len(sources)>256:raise LocalModelError(INVALID)
    paths, layers = [], set()
    for row in sources:
        if (not isinstance(row,dict) or set(row) != {'path','layer'}
                or not isinstance(row['layer'],str) or row['layer'] not in LAYERS
                or row['layer'] in layers or not isinstance(row['path'],str)):
            raise LocalModelError(INVALID)
        layers.add(row['layer']);paths.append(row['path'])
    evidence = source_evidence(paths)
    values = []
    for row in sources:
        proof = evidence[row['path']]
        if proof['state'] == 'absent':
            values.append({**row,'evidence':proof,'settings':{}});continue
        raw = secure_read(row['path'],os.getuid())
        if hashlib.sha256(raw).hexdigest() != proof['sha256']:raise LocalModelError(INVALID)
        value = (json.loads(raw,object_pairs_hook=_json_object) if provider == 'claude'
                 else tomllib.loads(raw.decode('utf-8')))
        if not isinstance(value,dict):raise LocalModelError(INVALID)
        if provider == 'claude':
            if set(value) - {'permissions'}:raise LocalModelError(UNSUPPORTED)
            permissions = value.get('permissions',{})
            if not isinstance(permissions,dict) or set(permissions)-{'allow','deny','ask'}:
                raise LocalModelError(UNSUPPORTED)
            # Even a read allow can overlap a task/Skill/managed rule. The
            # native grammar and precedence are not guessed from string suffixes.
            for rules in permissions.values():
                if not isinstance(rules,list):raise LocalModelError(INVALID)
                if rules:raise LocalModelError(UNSUPPORTED)
        else:
            if set(value)-{'approval_policy','sandbox_mode','sandbox_workspace_write'}:
                raise LocalModelError(UNSUPPORTED)
            if value.get('approval_policy','never') != 'never':raise LocalModelError(UNSUPPORTED)
            if value.get('sandbox_mode','workspace-write') != 'workspace-write':raise LocalModelError(UNSUPPORTED)
            sandbox = value.get('sandbox_workspace_write',{})
            if (not isinstance(sandbox,dict) or set(sandbox)-{'network_access','writable_roots',
                    'exclude_tmpdir_env_var','exclude_slash_tmp'}):raise LocalModelError(UNSUPPORTED)
            for key,item in sandbox.items():
                if key == 'writable_roots':
                    if item != []:raise LocalModelError(UNSUPPORTED)
                elif type(item) is not bool:raise LocalModelError(INVALID)
                elif item is not (key != 'network_access'):raise LocalModelError(UNSUPPORTED)
        values.append({**row,'evidence':proof,'settings':value})
    if source_evidence(paths) != evidence:raise LocalModelError(INVALID)
    return values


def _native_path(path):
    if (not isinstance(path,str) or not Path(path).is_absolute() or '..' in Path(path).parts
            or str(Path(path).resolve()) != path
            or any(char in path for char in '*?[]()\\\n\r\0')):
        raise LocalModelError(UNSUPPORTED)
    return '//'+path.lstrip('/')


def plan_native_scope(provider, scope, sources):
    """Return a deterministic, explicit proposal with unresolved native gates.

    No rules are normalized with a home-grown command parser. Bash entries are
    operator-selected native rule expressions, carried literally. Source
    unknowns fail closed instead of silently erasing ordinary configuration.
    """
    try:
        if provider not in ('codex','claude'):raise LocalModelError(UNSUPPORTED)
        if not isinstance(scope,dict) or set(scope) != SCOPE_FIELDS:raise LocalModelError(INVALID)
        if (scope['sandbox_auto_allow'] is not False or scope['inherited_bash_allow'] != []
                or scope['reservation_hook'] != 'enabled'
                or not isinstance(scope['cli_version'],str) or not scope['cli_version']):
            raise LocalModelError(INVALID)
        for key in ('read','edit','built_in_read','bash','mail_tools','settings_sources'):
            if (not isinstance(scope[key],list) or len(scope[key])>256
                    or any(not isinstance(v,str) or not v for v in scope[key])
                    or len(set(scope[key])) != len(scope[key])):raise LocalModelError(INVALID)
        from runtime_launch_intent import MAIL_TOOLS
        tools = scope['mail_tools']
        if (any(not re.fullmatch(r'mcp__[A-Za-z0-9_-]+__[a-z_]+',tool) for tool in tools)
                or {tool.rsplit('__',1)[1] for tool in tools} != MAIL_TOOLS
                or len({tool.rsplit('__',1)[0] for tool in tools}) != 1):
            raise LocalModelError(INVALID)
        records = _read_sources(provider,sources)
        if not set(scope['settings_sources']) <= {row['path'] for row in records}:
            raise LocalModelError(INVALID)
        paths = {key:[_native_path(path) for path in scope[key]] for key in ('read','edit','built_in_read')}
        if any(any(c in rule for c in '\n\r\0)') for rule in scope['bash']):
            raise LocalModelError(UNSUPPORTED)
        if provider == 'claude':
            allow = [f'{tool}({path})' for key,tool in (('read','Read'),('edit','Edit'))
                     for path in paths[key]]
            # A directory includes descendants only when that native pattern
            # is explicit in the proposal. This is not a permission ceiling.
            allow += [f'{tool}({path}/**)' for key,tool in (('read','Read'),('edit','Edit'))
                      for original,path in zip(scope[key],paths[key]) if Path(original).is_dir()]
            allow += ['Skill',*scope['mail_tools'],*[f'Bash({rule})' for rule in scope['bash']]]
            native = {'permissions':{'defaultMode':'dontAsk','allow':allow,'deny':[],'ask':[]},
                      'sandbox':{'autoAllowBashIfSandboxed':False}}
        else:
            # Workspace-write is NOT a per-command shell allowlist nor a
            # per-file Read/Edit plan. Do not quietly claim those restrictions.
            native = {'approval_policy':'never','sandbox_mode':'workspace-write',
                      'sandbox_workspace_write':{'network_access':False,
                          'exclude_tmpdir_env_var':True,'exclude_slash_tmp':True}}
        value = {'schema_version':1,'provider':provider,'declared_cli_version':scope['cli_version'],
                 'settings_sources':records,'requested_scope':deepcopy(scope),'native_settings':native,
                 'built_in_operations':{'status':'unverified','declared_read':deepcopy(scope['built_in_read'])},
                 'source_closure':{'status':'unverified'},'requires_native_validation':True,
                 'launch_admissible':False}
        value['plan_digest'] = canonical_digest(value)
        return value
    except LocalModelError:raise
    except (OSError,ValueError,TypeError,KeyError,UnicodeError):
        raise LocalModelError(INVALID) from None


class NativeConfigurationBinding:
    """Internal observation contract, never an assertion supplied by a caller.

    No implementation is adopted yet. A future binding must provide complete
    normalized observations and cannot launch/wait for a CLI while C is held.
    Comparison fixtures implementing this interface prove no native controls.
    """
    def observe_configuration(self,intent):
        raise LocalModelError(UNSUPPORTED)


def require_native_binding(plan):
    """No adopted capability binding exists yet; an assertion cannot enable it.

    A real native binding belongs to the later acceptance stage. This function
    deliberately accepts no caller supplied 'verified' boolean or CLI version.
    """
    raise LocalModelError(UNSUPPORTED)


def validate_native_proposal(plan, provider, scope):
    """Rebuild a stored proposal from current sources, never adopt its flags."""
    try:
        if set(scope)-SCOPE_FIELDS-{'native_proposal','framework_settings'}:raise LocalModelError(INVALID)
        original = {key:value for key,value in scope.items() if key in SCOPE_FIELDS}
        sources = [{key:record[key] for key in ('path','layer')} for record in plan['settings_sources']]
        if plan != plan_native_scope(provider,original,sources):raise LocalModelError(INVALID)
    except LocalModelError:raise
    except (KeyError,TypeError,ValueError):raise LocalModelError(INVALID) from None
