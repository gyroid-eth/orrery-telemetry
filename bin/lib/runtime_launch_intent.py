"""R3 immutable resolution, private owner storage and non-secret projections.

The caller supplies operator-approved context, never HTTP configuration input.
Native permissions, launch envelopes and transport belong to existing consumers.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import re
import sys

from local_model_auth import (LocalModelError, check_runner_resources, check_auth_source,
                              check_configuration_evidence, check_configuration_resources,
                              create_cli_acceptance, validate_cli_acceptance, intent_digest,
                              plain_text, secure_read, validate_auth_context)
from local_model_profiles import load_profile, profile_digest, selector_name
from runtime_intent_store import load_launch_intent, revise_launch_intent, save_launch_intent
from runtime_caller_schema import CALLER_FIELDS, canonical_caller
from runtime_provider_projection import _auth_command, _build_provider_projection, _toml_value

UNSUPPORTED = "LOCAL_MODEL_ADAPTER_UNSUPPORTED"
SCOPE_INVALID = "LOCAL_MODEL_PERMISSION_PLAN_UNSATISFIABLE"
MAIL_TOOLS = {"fetch_inbox", "send_message", "reserve_files", "renew_reservations", "release_reservations"}


def _configuration_metadata(caller,context):
    # Ordinary global resolution does not import a legacy-only dependency or
    # change its pinned renderer set. New policy/target inputs are explicit.
    if not {'execution_policy','legacy_context'} & context.keys():return {}
    from runtime_legacy_context import configuration_metadata
    return configuration_metadata(caller,context)


def _model_field(intent, resolved):
    """Read the two designed tags; never normalize or resolve a model again."""
    try:
        model = intent['model']
        if model['kind'] == 'catalog':value = model['id' if resolved else 'requested']
        elif model['kind'] == 'local-profile':value = model['resolved']['model'] if resolved else model['ref']
        else:raise ValueError()
        if not plain_text(value):raise ValueError()
        return value
    except (KeyError,TypeError,ValueError):
        raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED') from None


def model_reference(intent):
    return _model_field(intent,False)


def resolved_model_id(intent):
    return _model_field(intent,True)


def _scope_evidence(scope, context, *, provider):
    """Validate an already-approved plan, NOT implement native permissions.

    Version/source/expanded scope comparisons are necessary drift checks. A
    future native consumer must additionally prove the CLI enforces this scope.
    """
    try:
        if (scope["sandbox_auto_allow"] is not False or scope["inherited_bash_allow"] or
                scope["reservation_hook"] != "enabled" or not scope["cli_version"]):
            raise ValueError("unbounded scope")
        tools = scope["mail_tools"]
        if (not isinstance(tools, list) or len(set(tools)) != len(tools) or
                any(not re.fullmatch(r"mcp__[A-Za-z0-9_-]+__[a-z_]+", tool) for tool in tools) or
                {tool.rsplit("__", 1)[1] for tool in tools} != MAIL_TOOLS or
                len({tool.rsplit("__", 1)[0] for tool in tools}) != 1):
            raise ValueError("incomplete bound Mail")
        paths = scope["settings_sources"]
        approved = context.get("approved_settings_digests", {})
        if len(paths) == 1 and "approved_settings_sha256" in context:
            approved = {paths[0]: context["approved_settings_sha256"]}
        if set(approved) != set(paths):
            raise ValueError("settings approval missing")
        observed = {path: hashlib.sha256(secure_read(path, context["operator_uid"])).hexdigest()
                    for path in paths}
        if observed != approved:
            raise ValueError("settings changed")
        for key in ("read", "edit", "built_in_read"):
            if not isinstance(scope[key], list) or any(not Path(path).is_absolute() or ".." in Path(path).parts
                                                      or str(Path(path).resolve()) != path
                                                      for path in scope[key]):
                raise ValueError("noncanonical scope path")
        if not isinstance(scope["bash"], list):
            raise ValueError("missing native Bash plan")
        if 'native_proposal' in scope:
            from runtime_native_scope import validate_native_proposal
            validate_native_proposal(scope['native_proposal'],provider,scope)
        if 'framework_settings' in scope:
            script = context['runtime_client']
            if context.get('mode')=='legacy' and 'legacy_context' in context:
                from runtime_legacy_configuration import legacy_hook_settings
                expected=legacy_hook_settings(context['configuration_evidence']['runtime_python']['path'],
                    context['legacy_context']['install_root'],
                    context['legacy_context']['name'])
            else:
                from runtime_delivery import claude_hook_settings
                expected=claude_hook_settings(sys.executable,script,
                    Path(context['intent_owner']).with_name('runtime-client.json'))
            if (provider != 'claude' or script not in context['configuration_evidence']['renderer_sources']
                    or scope['framework_settings'] != expected):
                raise ValueError('framework hooks changed')
        return observed
    except (KeyError, TypeError, ValueError, OSError):
        raise LocalModelError(SCOPE_INVALID) from None


def _require_task_read(task, scope):
    target = Path(task)
    if not target.is_absolute() or ".." in target.parts or str(target.resolve()) != task or not any(
            target == Path(path) or (Path(path).is_dir() and target.is_relative_to(path))
            for path in scope["read"]):
        raise LocalModelError(SCOPE_INVALID)


def _resolution_inputs(caller, context):
    """Shared read-only admission; configure cannot choose a different schema."""
    caller = canonical_caller(caller)
    _configuration_metadata(caller,context)
    if (caller.get("effort") not in (None, "") or caller.get("strict_output_cap", False)):
        raise LocalModelError(UNSUPPORTED)
    provider = caller["provider"]
    selector_name(caller["model"])
    if context.get("identity", {}).get("name", caller["name"]) != caller["name"]:
        raise LocalModelError("LOCAL_MODEL_IDENTITY_MISMATCH")
    uid, root = context["operator_uid"], context["local_models_root"]
    profile = load_profile(root, caller["model"], expected_uid=uid)
    if provider not in profile:
        raise LocalModelError(UNSUPPORTED)
    scope = context.get("effective_scope")
    if scope is None:
        raise LocalModelError("LOCAL_MODEL_PERMISSION_PLAN_REQUIRED")
    evidence = _scope_evidence(scope, context,provider=provider)
    _require_task_read(caller["task"], scope)
    if 'configuration_evidence' in context:check_configuration_evidence(context['configuration_evidence'])
    return caller, profile, scope, evidence


def _new_intent(caller, context, profile, scope, evidence, auth):
    """Pure data builder, not permission/resource validation or persistence.

    Explicit configure may describe not-yet-published runner bytes in its plan.
    Apply must resolve normally after publication and compare the exact intent;
    no normal resolver flag disables resource checks for this planning purpose.
    """
    adapter = profile[caller['provider']]
    resolved = {"model": profile["model"], "context_window": profile["context_window"],
                "max_output_tokens": profile["max_output_tokens"], "adapter": deepcopy(adapter), "auth": auth}
    intent = {**deepcopy(caller), "schema_version": 1, "revision": 1,
              "operator_uid": context['operator_uid'], "local_models_root": str(context['local_models_root']),
              "model": {"kind": "local-profile", "ref": caller["model"],
                        "profile_sha256": profile_digest(profile), "resolved": resolved},
              "effective_scope": deepcopy(scope), "scope_evidence": evidence}
    if 'configuration_evidence' in context:
        intent['configuration_evidence'] = deepcopy(context['configuration_evidence'])
    intent.update(_configuration_metadata(caller,context))
    intent["intent_digest"] = intent_digest(intent)
    return intent


def resolve_launch_intent(caller, context, *, source=None):
    # source is diagnostic provenance only, never an alternative resolver.
    caller = canonical_caller(caller)
    _configuration_metadata(caller,context)
    if not caller['model'].startswith('local:'):
        from runtime_model_catalog import resolve_catalog_intent
        return resolve_catalog_intent(caller,context)
    caller, profile, scope, evidence = _resolution_inputs(caller, context)
    auth = None
    if 'auth_command' in profile[caller['provider']]:
        try:
            validate_auth_context(context['auth_context'])
            if context['auth_context']['cwd'] != str(context['local_models_root']):
                raise LocalModelError('LOCAL_MODEL_AUTH_FAILED')
            check_runner_resources(context['auth_runner'],context['operator_uid'])
            auth = {'context':deepcopy(context['auth_context']),'runner':deepcopy(context['auth_runner'])}
            if 'auth_source' in context:
                check_auth_source(context['auth_source'],profile[caller['provider']]['auth_command'],context['operator_uid'])
                auth['source'] = deepcopy(context['auth_source'])
        except (KeyError,TypeError):
            raise LocalModelError('LOCAL_MODEL_AUTH_FAILED') from None
    return _new_intent(caller,context,profile,scope,evidence,auth)


def make_launch_receipt(intent, *, phase):
    """Digest reference only, NOT a new receipt ledger or R2 launch decision."""
    return {"intent_digest": intent_digest(intent), "phase": phase}




def materialize_launch_intent(intent, context, *, operation, provider_session_id=None, caller_env=None,
                              _native_admission=False, _cli_acceptance=None):
    if operation not in ("spawn", "resume", "headless-wake"):
        raise LocalModelError(UNSUPPORTED)
    if (intent.get("intent_digest") != intent_digest(intent) or
            context["operator_uid"] != intent["operator_uid"]):
        raise LocalModelError("LOCAL_MODEL_INTENT_CHANGED")
    if {'execution_policy','legacy_context'} & (intent.keys()|context.keys()):
        caller={key:value for key,value in intent.items() if key in CALLER_FIELDS}
        caller['model']=model_reference(intent)
        # Preserve optional delivery semantics instead of erasing them.
        if 'task_delivery' in intent:caller['task_delivery']=intent['task_delivery']
        metadata=_configuration_metadata(caller,context)
        if metadata!={key:intent[key] for key in ('execution_policy','legacy_context') if key in intent}:
            raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
        # Preview admission is not connected here yet. In particular these
        # new data fields cannot change the existing strict native gate.
        if operation!='spawn' or _native_admission:
            raise LocalModelError(UNSUPPORTED)
    if 'task_sha256' in intent:
        # R2's fixed private task copy is the resume source; a removed original
        # temp task does not authorize re-resolution or a different hash.
        task = Path(intent['task'])
        if context.get('mode') == 'global' and context.get('client_root'):
            retained = Path(context['client_root'])/'runtime/task.md'
            if retained.exists() or retained.is_symlink():task = retained
        try:
            content = secure_read(task,context['operator_uid']) if task.exists() or task.is_symlink() else None
            observed = hashlib.sha256(content).hexdigest() if content is not None else None
            if observed != intent['task_sha256']:
                raise ValueError()
        except (OSError,TypeError,ValueError):raise LocalModelError('LOCAL_MODEL_TASK_CHANGED') from None
    if intent.get('model',{}).get('kind') == 'catalog':
        from runtime_model_catalog import materialize_catalog_intent
        return materialize_catalog_intent(intent,context,provider_session_id=provider_session_id)
    if str(context['local_models_root']) != intent['local_models_root']:
        raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
    profile = load_profile(context["local_models_root"], intent["model"]["ref"], expected_uid=context["operator_uid"])
    if profile_digest(profile) != intent["model"]["profile_sha256"]:
        raise LocalModelError("LOCAL_MODEL_PROFILE_CHANGED")
    resolved = intent["model"]["resolved"]
    expected = {key: profile[key] for key in ("model", "context_window", "max_output_tokens")}
    expected["adapter"] = profile.get(intent["provider"])
    if ({key: resolved.get(key) for key in expected} != expected or
            (resolved.get("auth") is not None) != ("auth_command" in expected["adapter"])):
        raise LocalModelError("LOCAL_MODEL_INTENT_CHANGED")
    if (context.get("effective_scope") != intent["effective_scope"] or
            _scope_evidence(intent["effective_scope"], context,provider=intent['provider']) != intent["scope_evidence"]):
        raise LocalModelError(SCOPE_INVALID)
    auth = intent["model"]["resolved"]["auth"]
    if 'configuration_evidence' in intent:
        if context.get('configuration_evidence') != intent['configuration_evidence']:
            raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
        check_configuration_resources(intent['configuration_evidence'])
    if auth is not None:
        if context.get("auth_context") != auth["context"] or context.get("auth_runner") != auth["runner"]:
            raise LocalModelError("LOCAL_MODEL_AUTH_FAILED")
        check_runner_resources(auth["runner"], context["operator_uid"])
        if 'source' in auth:
            if context.get('auth_source') != auth['source']:raise LocalModelError('LOCAL_MODEL_AUTH_FAILED')
            check_auth_source(auth['source'],resolved['adapter']['auth_command'],context['operator_uid'])
    if _cli_acceptance is not None:
        # A stale or forged receipt cannot be silently repaired by observing a
        # new version. Current bytes are checked AFTER the fresh observation.
        validate_cli_acceptance(intent,_cli_acceptance,check_current=False)
    proof = _cli_acceptance
    if _native_admission:
        import runtime_native_scope
        try:
            binding = runtime_native_scope.require_native_binding(intent['effective_scope']['native_proposal'])
        except LocalModelError:
            # Preserve existing strict drift reasons when no adopted observer
            # exists. Unchanged proposals still fail at the real native gate.
            check_configuration_evidence(intent['configuration_evidence'])
            raise
        if isinstance(binding,runtime_native_scope.NativeConfigurationBinding):
            proof = create_cli_acceptance(intent,binding.observe_configuration(deepcopy(intent)))
        elif binding is None:
            # Existing delivery-only TEST gate: no observation or byte exception.
            if proof is not None:raise LocalModelError('LOCAL_MODEL_CLI_CHANGED')
        else:raise LocalModelError(UNSUPPORTED)
    if 'configuration_evidence' in intent:
        check_configuration_evidence(intent['configuration_evidence'],intent=intent,cli_acceptance=proof)
    projection = _build_provider_projection(intent, context)
    if proof is not None:projection['cli_acceptance'] = proof
    return {**projection, "intent_digest": intent["intent_digest"],
            "profile_sha256": intent["model"]["profile_sha256"], "provider_session_id": provider_session_id}


def project_local_model(intent, *, usage=None):
    return {"kind": "local-profile", "ref": intent["model"]["ref"],
            "model": intent["model"]["resolved"]["model"], "context_usage": usage, "cloud_quota": None}
