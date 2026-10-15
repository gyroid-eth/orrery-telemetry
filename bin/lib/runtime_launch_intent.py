"""R3 local-profile core: immutable resolution and non-secret projections only.

No runtime-client persistence, admission, launch, native permissions, or HTTP
client lives here. The caller must supply operator-approved context, not accept
it from HTTP input. The existing launch consumers are deliberately unconnected.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shlex

from local_model_auth import (LocalModelError, check_runner_resources, intent_digest,
                              secure_read, validate_auth_context)
from local_model_profiles import load_profile, profile_digest, selector_name

UNSUPPORTED = "LOCAL_MODEL_ADAPTER_UNSUPPORTED"
SCOPE_INVALID = "LOCAL_MODEL_PERMISSION_PLAN_UNSATISFIABLE"
CALLER_FIELDS = {"provider", "model", "cwd", "task", "name", "parent", "tools", "base",
                 "clear_tools", "detached", "resources", "worktree", "chrome"}
MAIL_TOOLS = {"fetch_inbox", "send_message", "reserve_files", "renew_reservations", "release_reservations"}


def _scope_evidence(scope, context):
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
        return observed
    except (KeyError, TypeError, ValueError, OSError):
        raise LocalModelError(SCOPE_INVALID) from None


def _require_task_read(task, scope):
    target = Path(task)
    if not target.is_absolute() or ".." in target.parts or str(target.resolve()) != task or not any(
            target == Path(path) or (Path(path).is_dir() and target.is_relative_to(path))
            for path in scope["read"]):
        raise LocalModelError(SCOPE_INVALID)


def resolve_launch_intent(caller, context, *, source=None):
    # source is diagnostic provenance only, never an alternative resolver.
    if (set(caller) - CALLER_FIELDS - {"effort", "strict_output_cap"} or
            not CALLER_FIELDS <= caller.keys() or caller["provider"] not in ("codex", "claude") or
            caller.get("effort") not in (None, "") or caller.get("strict_output_cap", False)):
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
    evidence = _scope_evidence(scope, context)
    _require_task_read(caller["task"], scope)
    adapter = profile[provider]
    auth = None
    if "auth_command" in adapter:
        try:
            validate_auth_context(context["auth_context"])
            if context["auth_context"]["cwd"] != str(root):
                raise LocalModelError("LOCAL_MODEL_AUTH_FAILED")
            check_runner_resources(context["auth_runner"], uid)
            auth = {"context": deepcopy(context["auth_context"]), "runner": deepcopy(context["auth_runner"])}
        except (KeyError, TypeError):
            raise LocalModelError("LOCAL_MODEL_AUTH_FAILED") from None
    resolved = {"model": profile["model"], "context_window": profile["context_window"],
                "max_output_tokens": profile["max_output_tokens"], "adapter": deepcopy(adapter), "auth": auth}
    intent = {**deepcopy(caller), "schema_version": 1, "revision": 1,
              "operator_uid": uid, "local_models_root": str(root),
              "model": {"kind": "local-profile", "ref": caller["model"],
                        "profile_sha256": profile_digest(profile), "resolved": resolved},
              "effective_scope": deepcopy(scope), "scope_evidence": evidence}
    intent["intent_digest"] = intent_digest(intent)
    return intent


def make_launch_receipt(intent, *, phase):
    """Digest reference only, NOT a new receipt ledger or R2 launch decision."""
    return {"intent_digest": intent_digest(intent), "phase": phase}


def _auth_command(intent, context):
    auth = intent["model"]["resolved"]["auth"]
    if auth is None:
        return None
    # No guessed owner path or additional auth-plan file. The future existing
    # persistence owner must supply this reference explicitly.
    owner = context.get("intent_owner")
    if not isinstance(owner, str) or not Path(owner).is_absolute():
        raise LocalModelError("LOCAL_MODEL_INTENT_OWNER_REQUIRED")
    return [auth["runner"]["path"], "--intent", owner, "--digest", intent["intent_digest"],
            "--provider", intent["provider"]]


def _build_provider_projection(intent, context):
    """Return additive configuration; never rewrite instruction/skill settings.

    execution is the COMMON argument projection, not a runnable native launch.
    Startup/resume envelopes and native permission/config checks belong to R2/R3
    consumers. No ambient credential/provider/HOME variables are inherited.
    """
    model = intent["model"]
    resolved, provider = model["resolved"], intent["provider"]
    adapter = resolved["adapter"]
    command = _auth_command(intent, context)
    window, output = resolved["context_window"], resolved["max_output_tokens"]
    env = dict(adapter.get("env", {}))
    argv, settings = ["--model=" + resolved["model"]], {}
    name = selector_name(model["ref"])
    if provider == "codex":
        provider_id = "local_" + name
        definition = {"name": provider_id, "base_url": adapter["base_url"], "wire_api": "responses"}
        if command:
            auth = resolved["auth"]["context"]
            definition["auth"] = {"command": command[0], "args": command[1:], "cwd": auth["cwd"],
                                  "timeout_ms": auth["timeout_ms"], "refresh_interval_ms": auth["refresh_interval_ms"]}
        else:
            definition["requires_openai_auth"] = False
        config = {"model_provider": provider_id, "model_providers": {provider_id: definition},
                  "model_context_window": window, "model_auto_compact_token_limit": window - output}
        for key, value in config.items():
            if key == "model_providers":
                for field, setting in definition.items():
                    argv += ["-c", f"model_providers.{json.dumps(provider_id)}.{field}=" + _toml_value(setting)]
            else:
                argv += ["-c", key + "=" + _toml_value(value)]
        # Flat read projection for compatibility with the accepted fixture.
        provider_config = {**config, "wire_api": "responses"}
        permission_mode = None
    else:
        permission_mode = "dontAsk"
        argv += ["--permission-mode", permission_mode]
        env.update(ANTHROPIC_BASE_URL=adapter["base_url"], ANTHROPIC_MODEL=resolved["model"],
                   CLAUDE_CODE_SUBAGENT_MODEL=resolved["model"], CLAUDE_CODE_SUBAGENT_MODEL_FORCE="1",
                   CLAUDE_CODE_MAX_CONTEXT_TOKENS=str(window), CLAUDE_CODE_MAX_OUTPUT_TOKENS=str(output),
                   CLAUDE_CODE_API_KEY_HELPER_TTL_MS="300000", CLAUDE_CODE_SUBPROCESS_ENV_SCRUB="1")
        env.update({"ANTHROPIC_DEFAULT_" + tier + "_MODEL": resolved["model"]
                    for tier in ("OPUS", "SONNET", "HAIKU", "FABLE")})
        if command:
            settings["apiKeyHelper"] = shlex.join(command)
        provider_config = {"wire_api": "anthropic-messages", "settings": settings}
    return {"model": resolved["model"], "base_url": adapter["base_url"], "context_window": window,
            "max_output_tokens": output, "execution": {"argv": argv, "env": env, "cwd": intent["cwd"]},
            "provider_config": provider_config, "permission_mode": permission_mode,
            "effective_scope": deepcopy(intent["effective_scope"]), "auth_plan": deepcopy(resolved["auth"]),
            "inherit_env": False, "requires_native_validation": True, "warm_pool": False}


def _toml_value(value):
    if isinstance(value, dict):
        return "{ " + ", ".join(key + " = " + _toml_value(item) for key, item in value.items()) + " }"
    # JSON scalars/arrays used here are also valid TOML; ensure_ascii=False
    # avoids JSON's unsupported surrogate-pair escapes in TOML strings.
    return json.dumps(value, ensure_ascii=False)


def materialize_launch_intent(intent, context, *, operation, provider_session_id=None, caller_env=None):
    if operation not in ("spawn", "resume", "headless-wake"):
        raise LocalModelError(UNSUPPORTED)
    if (intent.get("intent_digest") != intent_digest(intent) or
            context["operator_uid"] != intent["operator_uid"] or
            str(context["local_models_root"]) != intent["local_models_root"]):
        raise LocalModelError("LOCAL_MODEL_INTENT_CHANGED")
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
            _scope_evidence(intent["effective_scope"], context) != intent["scope_evidence"]):
        raise LocalModelError(SCOPE_INVALID)
    auth = intent["model"]["resolved"]["auth"]
    if auth is not None:
        if context.get("auth_context") != auth["context"] or context.get("auth_runner") != auth["runner"]:
            raise LocalModelError("LOCAL_MODEL_AUTH_FAILED")
        check_runner_resources(auth["runner"], context["operator_uid"])
    projection = _build_provider_projection(intent, context)
    return {**projection, "intent_digest": intent["intent_digest"],
            "profile_sha256": intent["model"]["profile_sha256"], "provider_session_id": provider_session_id}


def project_local_model(intent, *, usage=None):
    return {"kind": "local-profile", "ref": intent["model"]["ref"],
            "model": intent["model"]["resolved"]["model"], "context_usage": usage, "cloud_quota": None}
