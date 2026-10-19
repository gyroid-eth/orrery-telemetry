"""Shared pure provider projection; no admission, discovery or execution."""
from copy import deepcopy
import json
from pathlib import Path
import shlex

from local_model_auth import LocalModelError
from local_model_profiles import selector_name



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
        if 'native_proposal' in intent['effective_scope']:
            config.update(deepcopy(intent['effective_scope']['native_proposal']['native_settings']))
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
        if 'native_proposal' in intent['effective_scope']:
            settings.update(deepcopy(intent['effective_scope']['native_proposal']['native_settings']))
            settings.update(deepcopy(intent['effective_scope'].get('framework_settings',{})))
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
