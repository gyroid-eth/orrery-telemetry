"""Public picker metadata from already checked, private native configuration.

Ordinary missing runtimes or a wrong connection mode must not advertise a
working launch. No credentials, filesystem discovery, registration or APIs here.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import runpy
import subprocess
import sys
import types

CONNECTIONS = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'hooks/native_connections.py'))


if __package__:
    from . import harness_helpers
    from .managed_capabilities import load_effort_capability, UNKNOWN
else:
    import harness_helpers
    from managed_capabilities import load_effort_capability, UNKNOWN


def _read(file):
    return json.loads(Path(file).read_text())


def catalog_providers(backends, availability, *, mode, enabled):
    """Project checked backend and harness metadata for the standard picker."""
    if enabled is not True:
        return []
    providers = []
    for backend_id, kind, spec in CONNECTIONS['declared_connections']({'backends': backends}):
        label = {'deepseek': 'DeepSeek', 'strata': 'Strata'}.get(backend_id, backend_id)
        locality = 'local' if kind == 'strata' else 'cloud'
        if not isinstance(spec, dict) or not isinstance(spec.get('model'), str) or not spec['model'].strip():
            continue
        readiness = availability.get(backend_id, {})
        harnesses = []
        for harness in ('dsh', 'hermes'):
            status = readiness.get(harness, {})
            ready = mode == 'legacy' and status.get('available') is True
            reason = None if ready else 'unsupported_mode' if mode != 'legacy' else status.get('reason') or 'runtime_not_prepared'
            harnesses.append({'id': harness, 'available': ready, 'default': False, 'reason': reason,
                              **{key: status.get(key, value) for key, value in UNKNOWN.items()}})
        first = next((h for h in harnesses if h['available']), None)
        if first:
            first['default'] = True
        provider = {
            'id': backend_id, 'kind': kind, 'label': spec.get('label') or label,
            'locality': spec.get('locality') or locality, 'server': spec.get('server') or label,
            'context': None, 'context_source': 'unreported', 'context_note': 'Server-reported context not available yet',
            'efforts': list((first or {}).get('efforts') or []),
            'default_model': spec['model'],
            'models': [{'id': spec['model'], 'label': spec['model'], 'harnesses': harnesses}],
        }
        if (first or {}).get('default_effort') in provider['efforts']:
            provider['effort_default'] = first['default_effort']
        providers.append(provider)
    return providers


def thin_launcher():
    # Import the product helper, never a provider runtime or its plugins.
    # An executable dashboard has only dashboard/ on sys.path. Give the
    # helpers their own package so relative imports also work in that mode.
    directory = Path(__file__).resolve().parents[1] / 'hooks'
    package = '_orrery_thin_hooks_' + hashlib.sha256(str(directory).encode()).hexdigest()[:12]
    name = package + '.harness_child'
    if name not in sys.modules:
        namespace = types.ModuleType(package)
        namespace.__path__ = [str(directory)]
        sys.modules.setdefault(package, namespace)
        spec = importlib.util.spec_from_file_location(name, directory / 'harness_child.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return sys.modules[name]


def _thin_selection(manifest, harness, backend, model):
    return thin_launcher().selection(manifest, harness, backend, model)


def load_standard_inputs(config_file, *, mode):
    """Admit plain native generations using the launcher's static check."""
    if not config_file:
        return {}, {}, False
    try:
        manifest = CONNECTIONS['read_manifest'](config_file)
        backends = manifest['backends']
        if manifest.get('enabled') is not True or not isinstance(backends, dict):
            return {}, {}, False
    except (OSError, ValueError, KeyError, TypeError):
        return {}, {}, False
    availability = {}
    for backend, kind, spec in CONNECTIONS['declared_connections'](manifest):
        availability[backend] = {}
        for model in harness_helpers.configured_models(manifest, backend):
            for harness in ('dsh', 'hermes'):
                reason, capability, runner = None, dict(UNKNOWN), None
                try:
                    if mode != 'legacy':
                        raise ValueError('unsupported_mode')
                    if sys.platform not in ('darwin', 'linux'):
                        raise ValueError('unsupported_platform')
                    if sys.platform == 'linux' and 'credential_ref' not in spec:
                        raise ValueError('credential_reader_unavailable')
                    opt_in = Path(__file__).with_name(harness + '-provider.json')
                    if opt_in.exists() and _read(opt_in).get('enabled') is not True:
                        raise ValueError('disabled_by_opt_in')
                    runner = _thin_selection(config_file, harness, backend, model)
                    template = _read(runner['config_file'])
                    capability = load_effort_capability(template, harness=harness,
                                                        backend_id=backend, model=model, backend_kind=kind)
                except ValueError as error:
                    # Only fixed product codes reach the public catalog.
                    code = str(error)
                    reason = code if re.fullmatch(r'[a-z][a-z0-9_]{0,63}', code) else 'runtime_not_prepared'
                except (OSError, ImportError, SyntaxError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
                    reason = 'runtime_not_prepared'
                availability[backend][(model, harness)] = {'available': reason is None,
                                                   'reason': reason, '_selection': runner if reason is None else None, **capability}
    return backends, availability, True


def standard_catalog(config_file, *, mode):
    """Standard spawn choices from the shared launcher admission checks."""
    backends, availability, enabled = load_standard_inputs(config_file, mode=mode)
    primary = {}
    for backend, statuses in availability.items():
        model = backends[backend].get('model')
        primary[backend] = {h: statuses.get((model, h), {}) if isinstance(model, str) else {}
                            for h in ('dsh', 'hermes')}
    providers = catalog_providers(backends, primary, mode=mode, enabled=enabled)
    manifest = {'backends': backends}
    for provider in providers:
        backend = provider['id']
        statuses = availability.get(backend, {})
        provider['models'] = harness_helpers.model_rows(manifest, backend,
            selections={pair: status.get('_selection') for pair, status in statuses.items()},
            statuses=statuses, product_root=Path(__file__).resolve().parents[1])
        ids = [row['id'] for row in provider['models']]
        if provider['default_model'] not in ids:
            provider['default_model'] = ids[0] if ids else None
    return [provider for provider in providers if provider['models']]
