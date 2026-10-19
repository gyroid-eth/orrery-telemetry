"""Public picker metadata from already checked, private managed configuration.

Ordinary missing runtimes or a wrong connection mode must not advertise a
working launch. No credentials, filesystem discovery, registration or APIs here.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def _read(file):
    return json.loads(Path(file).read_text())


def _executable(file):
    return isinstance(file, str) and Path(file).is_absolute() and Path(file).is_file() and os.access(file, os.X_OK)


def _node_ready(node):
    if not _executable(node):
        return False
    result = subprocess.run([node, '--version'], env={'PATH': '/usr/bin:/bin'},
                            capture_output=True, text=True, timeout=3)
    match = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)\s*', result.stdout)
    return result.returncode == 0 and bool(match) and tuple(map(int, match.groups())) >= (24, 2, 0)


def _dsh_ready(template, model, backend_id):
    backend = _read(template['backend_file'])['backends'][backend_id]
    if backend['model'] != model:
        return 'model_mismatch'
    if not backend.get('base_url', '').startswith('https://') or not all(backend.get('keychain', {}).get(k) for k in ('service', 'account')):
        return 'backend_not_configured'
    if not _node_ready(template['node']):
        return 'node_not_prepared'
    packages = Path(template['runtime']) / 'node_modules'
    for package, version in [('@deepseek-ai/dsh', '0.2.1-rc.2'), ('@deepseek-harness-tui/dsh-tui', '0.16.1')]:
        if _read(packages / package / 'package.json').get('version') != version:
            return 'runtime_version_mismatch'
    integration = Path(__file__).resolve().parents[1] / 'integrations/dsh'
    recipe_file = integration / 'tui-managed.patch'
    recipe, receipt = _read(recipe_file), _read(template['patch_receipt'])
    files = sorted({row['file'] for row in recipe['replacements']})
    if (receipt.get('recipe_sha256') != hashlib.sha256(recipe_file.read_bytes()).hexdigest()
            or sorted(row['file'] for row in receipt['changes']) != files):
        return 'patch_not_prepared'
    for row in receipt['changes']:
        relative = Path(row['file'])
        if relative.is_absolute() or '..' in relative.parts:
            return 'patch_not_prepared'
        if hashlib.sha256((packages / '@deepseek-harness-tui/dsh-tui' / relative).read_bytes()).hexdigest() != row['after']:
            return 'patch_not_prepared'
    return None


def _hermes_ready(template, model, backend_id):
    spec = template['profiles'][backend_id]
    if not _executable(template['hermes_executable']):
        return 'runtime_not_prepared'
    source = Path(template['hermes_source']).resolve()
    install = Path(template['managed_root']).resolve() / 'installs' / hashlib.sha256(str(source).encode()).hexdigest()[:16]
    environment = Path(_read(install / 'facts.json')['packages']['venv']['environment']).resolve()
    if (not environment.is_relative_to((install / 'environments').resolve())
            or not (environment / 'pyvenv.cfg').is_file() or not _executable(str(environment / 'bin/python'))):
        return 'runtime_not_prepared'
    effective = spec
    if not all(spec.get(k) for k in ('model', 'base_url', 'key_cmd')) and spec.get('template'):
        # Use the existing safe template projection, without loading providers,
        # creating a profile, or evaluating the key-command reference.
        projection = Path(__file__).resolve().parents[1] / 'integrations/hermes/profile_projection.py'
        code = ('import json,runpy,sys; from pathlib import Path; '
                'sys.path.insert(0,sys.argv[2]); m=runpy.run_path(sys.argv[1]); '
                'p=m["project"](json.loads(sys.argv[3]),Path(sys.argv[4]),["title_generation","background_review"]); '
                'r=p["profile_config"]["providers"][p["provider"]]; '
                'print(json.dumps({"model":p["model"],"base_url":r["base_url"],"key_cmd":bool(r["key_cmd"])}))')
        completed = subprocess.run([str(environment / 'bin/python'), '-I', '-B', '-c', code,
            str(projection), str(source), json.dumps(spec), str(Path(template['managed_root']) / 'catalog-preview')],
            env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True, timeout=5)
        if completed.returncode:
            return 'backend_not_configured'
        effective = json.loads(completed.stdout)
    if effective.get('model') != model:
        return 'model_mismatch'
    if not (effective.get('base_url') or effective.get('api') or '').startswith('https://') or not effective.get('key_cmd'):
        return 'backend_not_configured'
    return None


def load_catalog_inputs(config_file, *, mode):
    """Read operator-owned references; missing opt-in is off, errors are codes.

    This never reads a credential, imports a provider, repairs an installation,
    or contacts Mail/provider services. Full keyless preflight still runs at POST.
    """
    if not config_file or not Path(config_file).is_file():
        return {}, {}, False
    try:
        manifest = _read(config_file)
        if manifest.get('enabled') is not True:
            return {}, {}, False
        backends, runners = manifest['backends'], manifest['harnesses']
        if not isinstance(backends, dict) or not isinstance(runners, dict):
            return {}, {}, False
        availability = {}
        for backend_id, spec in backends.items():
            if backend_id not in ('deepseek', 'strata'):
                continue
            availability[backend_id] = {}
            for harness in ('dsh', 'hermes'):
                reason = ('unsupported_mode' if mode != 'legacy' else
                          'unsupported_platform' if sys.platform != 'darwin' else 'runtime_not_prepared')
                if mode == 'legacy' and sys.platform == 'darwin' and harness in runners:
                    try:
                        runner = runners[harness]
                        if not _executable(runner['python']) or not Path(runner['config_file']).is_absolute():
                            raise ValueError('Runner references not prepared')
                        template = _read(runner['config_file'])
                        root = Path(template['product_root'])
                        if not all((root / name).is_file() for name in (
                                'bin/agentstack-preregister-child', 'hooks/child_tools.py',
                                'hooks/cleanup-child-agent.sh', 'integrations/codex_app/plugin/scripts/run-mcp.sh')):
                            raise ValueError('Product helpers missing')
                        if not all(_executable(str(root / name)) for name in (
                                'bin/agentstack-preregister-child', 'integrations/codex_app/plugin/scripts/run-mcp.sh')):
                            raise ValueError('Product helpers are not executable')
                        local_cli = Path(__file__).resolve().parents[1] / 'integrations' / harness / ('launch.py' if harness == 'dsh' else 'runner.py')
                        if not local_cli.is_file():
                            raise ValueError('Runner CLI missing')
                        reason = (_dsh_ready if harness == 'dsh' else _hermes_ready)(template, spec['model'], backend_id)
                    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                        reason = 'runtime_not_prepared'
                availability[backend_id][harness] = {'available': reason is None, 'reason': reason}
        return backends, availability, True
    except (OSError, ValueError, KeyError, TypeError):
        return {}, {}, False


def catalog_providers(backends, availability, *, mode, enabled):
    """Return only the explicit managed providers in the A1 catalog contract."""
    if enabled is not True:
        return []
    providers = []
    for backend_id, label, locality in [('deepseek', 'DeepSeek', 'cloud'), ('strata', 'Strata', 'local')]:
        spec = backends.get(backend_id)
        if not isinstance(spec, dict) or not isinstance(spec.get('model'), str) or not spec['model'].strip():
            continue
        readiness = availability.get(backend_id, {})
        harnesses = []
        for harness in ('dsh', 'hermes'):
            status = readiness.get(harness, {})
            ready = mode == 'legacy' and status.get('available') is True
            reason = None if ready else 'unsupported_mode' if mode != 'legacy' else status.get('reason') or 'runtime_not_prepared'
            harnesses.append({'id': harness, 'available': ready, 'default': False, 'reason': reason})
        first = next((h for h in harnesses if h['available']), None)
        if first:
            first['default'] = True
        provider = {
            'id': backend_id, 'label': spec.get('label') or label, 'managed': True,
            'locality': spec.get('locality') or locality, 'server': spec.get('server') or label,
            'context': None, 'context_source': 'unreported', 'context_note': 'Server-reported context not available yet',
            'helper_models': [{'id': 'same', 'label': 'Same as main'}],
            'efforts': list(spec.get('efforts') or []),
            'default_model': spec['model'],
            'models': [{'id': spec['model'], 'label': spec['model'], 'harnesses': harnesses}],
        }
        if spec.get('default_effort') in provider['efforts']:
            provider['effort_default'] = spec['default_effort']
        providers.append(provider)
    return providers
