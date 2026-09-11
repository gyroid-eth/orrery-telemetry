#!/usr/bin/env python3
"""Provider-aware Dashboard entry point.

The canonical ``dashboard/server.py`` remains untouched. Optional provider
payloads can extend the control plane here while the service runner falls back
to the core server when this file is not installed.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
from typing import Any, Callable


_HERE = pathlib.Path(__file__).resolve().parent
_CORE_PATH = _HERE / "server.py"
_CORE_MODULE_NAME = "_orrery_provider_dashboard_core"


def _load_core():
    existing = sys.modules.get(_CORE_MODULE_NAME)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(_CORE_MODULE_NAME, _CORE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load dashboard core: {_CORE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_CORE_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_CORE_MODULE_NAME, None)
        raise
    return module


def _replace_required(text: str, old: str, new: str, *, label: str) -> str:
    if old not in text:
        raise RuntimeError(f"provider dashboard patch target missing: {label}")
    return text.replace(old, new, 1)


def _install_explicit_effort_policy(base: Any) -> None:
    """Make provider-declared effort requirements explicit in the Dashboard.

    Gemini's direct CLI launcher keeps its historical ``high`` fallback, but
    the Dashboard control plane must not silently choose an effort on behalf of
    the user. The provider catalog therefore declares effort as required and
    the rendered modal starts with no effort selected.
    """
    if getattr(base, "_GEMINI_EXPLICIT_EFFORT_POLICY_INSTALLED", False):
        return

    original_catalog = base.spawn_names_payload

    def spawn_names_payload() -> dict:
        payload = dict(original_catalog())
        providers = []
        for provider in payload.get("providers") or []:
            item = dict(provider)
            if item.get("id") == "gemini":
                capabilities = dict(item.get("capabilities") or {})
                capabilities["effort_required"] = True
                item["capabilities"] = capabilities
                item["effort_default"] = ""
            providers.append(item)
        payload["providers"] = providers
        return payload

    base.spawn_names_payload = spawn_names_payload

    original_spawn = base.do_spawn

    def do_spawn(payload: dict) -> dict:
        provider = str(payload.get("provider") or "claude").strip().lower()
        if provider == "gemini" and not str(payload.get("effort") or "").strip():
            return {"ok": False, "error": "effort required for provider gemini"}
        return original_spawn(payload)

    base.do_spawn = do_spawn

    original_render: Callable[..., bytes] = base._render_dashboard_index

    def _render_dashboard_index(
        source: bytes, language: str = "", murmur: str = ""
    ) -> bytes:
        rendered = original_render(source, language, murmur)
        text = rendered.decode("utf-8")

        text = _replace_required(
            text,
            """    return {id,label:String(provider&&provider.label||id).trim(),
      models,defaultModel,efforts,
      defaultEffort:String(provider&&provider.effort_default||'').trim()};""",
            """    const capabilities=(provider&&provider.capabilities&&typeof provider.capabilities==='object')
      ?provider.capabilities:{};
    return {id,label:String(provider&&provider.label||id).trim(),
      models,defaultModel,efforts,
      defaultEffort:String(provider&&provider.effort_default||'').trim(),
      capabilities};""",
            label="provider capability normalization",
        )

        text = _replace_required(
            text,
            """  const fallback=efforts.includes(provider&&provider.defaultEffort)
    ? provider.defaultEffort:(efforts[0]||'');
  selectSpawnEffort(fallback);""",
            """  const effortRequired=!!(provider&&provider.capabilities&&provider.capabilities.effort_required);
  const fallback=effortRequired?'':(
    efforts.includes(provider&&provider.defaultEffort)
      ?provider.defaultEffort:(efforts[0]||''));
  selectSpawnEffort(fallback);""",
            label="explicit effort selection",
        )

        text = _replace_required(
            text,
            """  hint.textContent=copy?`${spmSelectedEffort} · ${copy}`:'';
  renderSpawnEngineNote();
}

function renderSpawnEngineNote(){""",
            """  hint.textContent=copy?`${spmSelectedEffort} · ${copy}`:'';
  renderSpawnEngineNote();
  updateSpawnButton();
}

function renderSpawnEngineNote(){""",
            label="effort button refresh",
        )

        text = _replace_required(
            text,
            """  const resourcesReady=!providerCaps.resources_required||!!(SPM('spm-resources')&&SPM('spm-resources').value.split(',').some(item=>item.trim()));
  button.disabled=spmBusy||!spmReady||!identityReady||!resourcesReady;""",
            """  const resourcesReady=!providerCaps.resources_required||!!(SPM('spm-resources')&&SPM('spm-resources').value.split(',').some(item=>item.trim()));
  const effortReady=!providerCaps.effort_required||!!spmSelectedEffort;
  button.disabled=spmBusy||!spmReady||!identityReady||!resourcesReady||!effortReady;""",
            label="effort readiness gate",
        )
        return text.encode("utf-8")

    base._render_dashboard_index = _render_dashboard_index
    base._GEMINI_EXPLICIT_EFFORT_POLICY_INSTALLED = True


_base = _load_core()

try:
    from dashboard.gemini_provider_runtime import install as _install_gemini
except ModuleNotFoundError:  # direct `python dashboard/provider_server.py`
    from gemini_provider_runtime import install as _install_gemini

_install_gemini(_base)
_install_explicit_effort_policy(_base)

if __name__ == "__main__":
    _base.main()
else:
    # Provider-specific tests/callers receive the patched isolated core while
    # importing dashboard.server directly still returns the canonical core.
    sys.modules[__name__] = _base
