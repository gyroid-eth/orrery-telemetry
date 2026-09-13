#!/usr/bin/env python3
"""Provider-aware Dashboard entry point.

The canonical ``dashboard/server.py`` stays the core.  Optional provider
payloads extend the control plane here, and the service runner falls back to
the core server when this file is not installed.  Provider policy lives in the
extension modules themselves; this file only loads them.
"""
from __future__ import annotations

import importlib.util
import logging
import pathlib
import sys


_HERE = pathlib.Path(__file__).resolve().parent
_CORE_PATH = _HERE / "server.py"
_CORE_MODULE_NAME = "_orrery_provider_dashboard_core"
# (module name, sibling file) for each optional provider extension.
_EXTENSIONS = (
    ("_orrery_provider_dashboard_gemini", "gemini_provider_runtime.py"),
)


def _load_module(name: str, path: pathlib.Path):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load dashboard module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _install_extensions(base) -> list[str]:
    """Install every loadable extension; return the ones that failed.

    A broken or partially installed provider payload must not take the
    canonical Claude/Codex dashboard down with it.  The failing provider is
    simply absent from the catalog and cannot be spawned.
    """
    failed = []
    for name, filename in _EXTENSIONS:
        path = _HERE / filename
        try:
            _load_module(name, path).install(base)
        except Exception:  # noqa: BLE001 - reported, then the core keeps serving
            logging.getLogger("agentstack.dashboard.providers").exception(
                "optional dashboard provider extension disabled: %s", path
            )
            failed.append(filename)
    return failed


_base = _load_module(_CORE_MODULE_NAME, _CORE_PATH)
_base._PROVIDER_EXTENSION_FAILURES = _install_extensions(_base)

if __name__ == "__main__":
    # In the service process this core is the only one: code that later
    # imports the dashboard core by name reaches the same module state.
    sys.modules.setdefault("server", _base)
    sys.modules.setdefault("dashboard.server", _base)
    _base.main()
else:
    # Provider-specific tests/callers receive the patched isolated core while
    # importing dashboard.server directly still returns the canonical core.
    sys.modules[__name__] = _base
