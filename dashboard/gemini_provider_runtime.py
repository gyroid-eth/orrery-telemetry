"""Add Antigravity / Gemini to the Dashboard NEW AGENT control plane.

The core dashboard stays provider-agnostic.  This optional extension is loaded
only by ``provider_server.py`` when the Gemini provider payload is installed.
It reuses the existing registration/contact/readiness pipeline and dispatches
only the provider-specific launch step to ``spawn_gemini_preregistered.sh``.
"""
from __future__ import annotations

import os
import shlex
import tempfile
import threading
from typing import Any


_PATCH_LOCK = threading.Lock()
_GEMINI_DEFAULT_MODELS = (
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium",
)
_GEMINI_EFFORTS = ("low", "medium", "high")
_GEMINI_DEFAULT_EFFORT = "high"


def _gemini_models() -> list[str]:
    values = [
        value.strip()
        for value in os.environ.get("AGENTSTACK_GEMINI_MODELS", "").split(",")
        if value.strip()
    ]
    return values or list(_GEMINI_DEFAULT_MODELS)


def _catalog_item() -> dict:
    models = _gemini_models()
    return {
        "id": "gemini",
        "label": "Antigravity",
        "program": "antigravity",
        "models": models,
        "default_model": models[0],
        "efforts": list(_GEMINI_EFFORTS),
        "effort_default": _GEMINI_DEFAULT_EFFORT,
        "provider_key": "google",
        "capabilities": {
            "effort": True,
            "mcp": True,
            "resume": False,
            "runtime": True,
            "transcript": False,
            "standalone": False,
            "worktree_required": True,
            "resources_required": True,
        },
    }


def _normalize_resources(raw: str) -> str:
    items = [item.strip() for item in (raw or "").split(",") if item.strip()]
    if not items:
        raise ValueError("resources required for provider gemini")
    return ",".join(items)


def _write_task_file(base: Any, task: str) -> str:
    os.makedirs(base.RUNTIME_DIR, mode=0o700, exist_ok=True)
    fd, path = tempfile.mkstemp(
        prefix="provider-gemini-task-",
        suffix=".txt",
        dir=base.RUNTIME_DIR,
        text=True,
    )
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(task)
    return path


def _write_adapter_wrapper(
    base: Any,
    adapter_script: str,
    *,
    model: str,
    effort: str,
    resources: str,
    task_file: str,
) -> str:
    os.makedirs(base.RUNTIME_DIR, mode=0o700, exist_ok=True)
    fd, wrapper = tempfile.mkstemp(
        prefix="provider-gemini-",
        suffix=".sh",
        dir=base.RUNTIME_DIR,
        text=True,
    )
    cleanup = " ".join(shlex.quote(path) for path in (wrapper, task_file))
    exports = {
        "AGENTSTACK_GEMINI_MODEL": model,
        "AGENTSTACK_GEMINI_EFFORT": effort,
        "AGENTSTACK_GEMINI_RESOURCES": resources,
        "AGENTSTACK_GEMINI_TASK_FILE": task_file,
    }
    body = ["#!/bin/bash", "set -u"]
    body.extend(
        f"export {key}={shlex.quote(value)}" for key, value in exports.items()
    )
    body.extend(
        [
            f"{shlex.quote(adapter_script)} \"$@\"",
            "status=$?",
            f"rm -f -- {cleanup}",
            'exit "$status"',
            "",
        ]
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(body))
    os.chmod(wrapper, 0o700)
    return wrapper


def _install_catalog(base: Any) -> None:
    original = base.spawn_names_payload

    def spawn_names_payload() -> dict:
        payload = original()
        providers = list(payload.get("providers") or [])
        if not any(item.get("id") == "gemini" for item in providers):
            providers.append(_catalog_item())
        payload["providers"] = providers
        return payload

    base.spawn_names_payload = spawn_names_payload


def _install_spawn(base: Any) -> None:
    original = base.do_spawn

    def do_spawn(payload: dict) -> dict:
        provider = str(payload.get("provider") or "claude").strip().lower()
        if provider != "gemini":
            # Gemini dispatch temporarily swaps two legacy core globals below.
            # Native launches share the lock so they can never observe those
            # provider-specific values under concurrent Dashboard requests.
            with _PATCH_LOCK:
                return original(payload)

        if "standalone" in payload and not isinstance(payload["standalone"], bool):
            return {"ok": False, "error": "standalone must be boolean"}
        if payload.get("standalone", False):
            return {
                "ok": False,
                "error": "standalone not supported for provider gemini",
            }

        models = _gemini_models()
        model = str(payload.get("model") or models[0]).strip()
        if model not in models:
            return {
                "ok": False,
                "error": f"model not allowed for provider gemini: {model}",
            }
        effort = str(payload.get("effort") or _GEMINI_DEFAULT_EFFORT).strip().lower()
        if effort not in _GEMINI_EFFORTS:
            return {
                "ok": False,
                "error": f"effort not allowed for provider gemini: {effort}",
            }
        try:
            resources = _normalize_resources(str(payload.get("resources") or ""))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        adapter_script = os.path.join(base.HOOKS_DIR, "spawn_gemini_preregistered.sh")
        if not os.path.exists(adapter_script):
            return {
                "ok": False,
                "error": f"spawn adapter missing for provider gemini: {adapter_script}",
            }

        task_file = ""
        wrapper = ""
        handed_off = False
        try:
            task_file = _write_task_file(base, str(payload.get("task") or ""))
            wrapper = _write_adapter_wrapper(
                base,
                adapter_script,
                model=model,
                effort=effort,
                resources=resources,
                task_file=task_file,
            )

            translated = dict(payload)
            translated.update(
                provider="claude",
                model=model,
                effort="",
                worktree=True,
            )
            translated.pop("resources", None)

            with _PATCH_LOCK:
                old_script = base.SPAWN_SCRIPT
                old_model = base._SPAWN_MODELS.get(model)
                try:
                    base.SPAWN_SCRIPT = wrapper
                    base._SPAWN_MODELS[model] = ("antigravity", model)
                    result = original(translated)
                finally:
                    base.SPAWN_SCRIPT = old_script
                    if old_model is None:
                        base._SPAWN_MODELS.pop(model, None)
                    else:
                        base._SPAWN_MODELS[model] = old_model

            if result.get("ok"):
                result.update(
                    provider="gemini",
                    model=model,
                    effort=effort,
                    worktree=True,
                )
                # Popen already owns the wrapper path.  The wrapper removes
                # itself and the task file after the asynchronous adapter exits.
                handed_off = True
            return result
        finally:
            if not handed_off:
                for path in (wrapper, task_file):
                    if not path:
                        continue
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass

    base.do_spawn = do_spawn


def _inject_ui_capabilities(text: str) -> str:
    """Add capability-driven resource/worktree controls to the existing modal."""
    isolation = """          <div class=\"spm-row full\">\n            <label class=\"spm-lab\">isolation</label>"""
    if 'id="spm-resources-row"' not in text and isolation in text:
        resource_row = """          <div class=\"spm-row full\" id=\"spm-resources-row\" style=\"display:none\">\n            <label class=\"spm-lab\" for=\"spm-resources\">resources</label>\n            <input type=\"text\" id=\"spm-resources\" placeholder=\"src/**,tests/**\" autocomplete=\"off\">\n            <div class=\"spm-hint\">Comma-separated paths reserved for providers that require resource isolation.</div>\n          </div>\n"""
        text = text.replace(isolation, resource_row + isolation, 1)

    provider_marker = """  spmSelectedEffort='';\n  SPM('spm-providers').querySelectorAll('.spm-provider-tab').forEach(btn=>{"""
    if (
        "const providerCaps=provider&&provider.capabilities||{};" not in text
        and provider_marker in text
    ):
        provider_replacement = """  spmSelectedEffort='';\n  const providerCaps=provider&&provider.capabilities||{};\n  if(SPM('spm-resources-row'))SPM('spm-resources-row').style.display=providerCaps.resources_required?'grid':'none';\n  if(SPM('spm-resources'))SPM('spm-resources').oninput=updateSpawnButton;\n  if(SPM('spm-worktree')){\n    if(providerCaps.worktree_required)SPM('spm-worktree').checked=true;\n    SPM('spm-worktree').disabled=!!providerCaps.worktree_required;\n    SPM('spm-wt-base').classList.toggle('on',SPM('spm-worktree').checked);\n  }\n  SPM('spm-providers').querySelectorAll('.spm-provider-tab').forEach(btn=>{"""
        text = text.replace(provider_marker, provider_replacement, 1)

    payload_marker = """    group:SPM('spm-group').value.trim()\n  };"""
    if "payload.resources=resources" not in text and payload_marker in text:
        payload_replacement = """    group:SPM('spm-group').value.trim()\n  };\n  const resources=(SPM('spm-resources')&&SPM('spm-resources').value||'').trim();\n  if(resources)payload.resources=resources;"""
        text = text.replace(payload_marker, payload_replacement, 1)

    button_marker = """  const identityReady=!spmSelectedName||spmIdentityState==='verified';\n  button.disabled=spmBusy||!spmReady||!identityReady;"""
    if (
        "const resourcesReady=!providerCaps.resources_required" not in text
        and button_marker in text
    ):
        button_replacement = """  const identityReady=!spmSelectedName||spmIdentityState==='verified';\n  const selectedProvider=spmProviders.find(item=>item.id===spmSelectedProvider);\n  const providerCaps=selectedProvider&&selectedProvider.capabilities||{};\n  const resourcesReady=!providerCaps.resources_required||!!(SPM('spm-resources')&&SPM('spm-resources').value.split(',').some(item=>item.trim()));\n  button.disabled=spmBusy||!spmReady||!identityReady||!resourcesReady;"""
        text = text.replace(button_marker, button_replacement, 1)

    reset_marker = """  SPM('spm-worktree').checked=false;\n  SPM('spm-wt-base').classList.remove('on');"""
    if "SPM('spm-resources').value=''" not in text and reset_marker in text:
        reset_replacement = """  SPM('spm-worktree').checked=false;\n  SPM('spm-worktree').disabled=false;\n  if(SPM('spm-resources'))SPM('spm-resources').value='';\n  SPM('spm-wt-base').classList.remove('on');"""
        text = text.replace(reset_marker, reset_replacement, 1)
    return text


def _install_render(base: Any) -> None:
    original = base._render_dashboard_index

    def _render_dashboard_index(
        source: bytes, language: str = "", murmur: str = ""
    ) -> bytes:
        rendered = original(source, language, murmur)
        text = rendered.decode("utf-8")
        return _inject_ui_capabilities(text).encode("utf-8")

    base._render_dashboard_index = _render_dashboard_index


def install(base: Any) -> Any:
    if getattr(base, "_GEMINI_PROVIDER_RUNTIME_INSTALLED", False):
        return base
    _install_catalog(base)
    _install_spawn(base)
    _install_render(base)
    base._GEMINI_PROVIDER_RUNTIME_INSTALLED = True
    return base
