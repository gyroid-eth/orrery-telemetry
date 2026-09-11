#!/usr/bin/env python3
"""Provider-aware Dashboard entry point.

The canonical ``dashboard/server.py`` remains untouched.  Optional provider
payloads can extend the control plane here while the service runner falls back
to the core server when this file is not installed.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys


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


_base = _load_core()

try:
    from dashboard.gemini_provider_runtime import install as _install_gemini
except ModuleNotFoundError:  # direct `python dashboard/provider_server.py`
    from gemini_provider_runtime import install as _install_gemini

_install_gemini(_base)

if __name__ == "__main__":
    _base.main()
else:
    # Provider-specific tests/callers receive the patched isolated core while
    # importing dashboard.server directly still returns the canonical core.
    sys.modules[__name__] = _base
