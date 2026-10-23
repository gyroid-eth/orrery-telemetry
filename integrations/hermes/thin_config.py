"""Generate native Hermes configuration without acquiring credentials.

Read-only admission is shared by the launcher and catalog. The CLI emits a
native argv/env plan; Hermes owns its session, tools, provider and lifetime.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import sys
from urllib.parse import urlsplit

THIN_CONFIG_CONTRACT = 1
HELPER_MODEL_CONTRACT = 1
CONNECTIONS = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'hooks/native_connections.py'))
PLUGIN = 'orrery-thin-guard'
EFFORTS = {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'}


def read(path):
    text = Path(path).read_text(encoding='utf-8')
    try:
        return json.loads(text, object_pairs_hook=CONNECTIONS['_unique_pairs'])
    except json.JSONDecodeError:
        # Data parser only: never import Hermes (which can bootstrap at import).
        try:
            from ruamel.yaml import YAML
        except ModuleNotFoundError:
            # Match telemetry_defaults in the repository development environment.
            from yaml import safe_load
            return safe_load(text) or {}
        parser = YAML(typ='safe')
        parser.version = (1, 1)
        return parser.load(text) or {}


def _provider_ids(source):
    tree = ast.parse((source / 'hermes_cli/auth.py').read_text())
    rows = next(node.value for node in tree.body if isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name) and node.target.id == '_REGISTRY_ROWS')
    names = {'openrouter'}
    for row in rows.elts:
        first = row.elts[0] if isinstance(row, ast.Tuple) else row.args[0]
        names.add(ast.literal_eval(first))
    return names


def validate_input(document):
    """Read-only static selection. Raises fixed reasons, never helper output."""
    try:
        return _validate(document)
    except ValueError as error:
        if str(error) in {'unknown_backend', 'model_mismatch', 'invalid_effort',
                          'builtin_provider_collision', 'invalid_runtime', 'invalid_route',
                          'invalid_helper_model', 'invalid_resume_session_id',
                          'invalid_credential_ref', 'credential_reference_conflict',
                          'unsupported_credential_store', 'credential_reader_unavailable',
                          'invalid_connection_kind', 'connection_kind_changed',
                          'ambiguous_connection_configuration'}:
            raise
        raise ValueError('invalid_runtime') from None
    except (OSError, KeyError, TypeError, StopIteration, SyntaxError, AttributeError):
        raise ValueError('invalid_runtime') from None


def profile_route(spec, backend):
    """Project route metadata; launch admission validates credentials separately.

    Secret Service key_cmd comes from the manifest and is synthesized only by
    _validate. Metadata must not require a legacy profile command to read mode.
    """
    inherited = read(spec['template']) if spec.get('template') else {}
    provider = spec.get('provider') or inherited.get('model', {}).get('provider')
    if isinstance(provider, str):
        provider = provider.strip()
    entry = inherited.get('providers', {}).get(provider, {})
    if not entry:
        entry = next((row for row in inherited.get('custom_providers', []) if row.get('name') == provider), {})
    route = {key: entry[key] for key in ('base_url', 'api', 'api_mode', 'transport', 'model', 'key_cmd') if key in entry}
    route.update({key: spec[key] for key in ('base_url', 'api', 'api_mode', 'transport', 'model', 'key_cmd') if key in spec})
    default_model = spec.get('model') or inherited.get('model', {}).get('default') or route.get('model')
    base = route.get('base_url') or route.get('api') or ''
    url = urlsplit(base)
    mode = route.get('api_mode') or route.get('transport')
    command = route.get('key_cmd')
    if (url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment
            or mode not in ('chat_completions', 'anthropic_messages')):
        raise ValueError('invalid_route')
    # This pinned Anthropic converter discards DeepSeek's signed thinking.
    # The native OpenAI-compatible transport preserves reasoning across tools.
    if (backend == 'deepseek' and mode == 'anthropic_messages'
            and url.hostname == 'api.deepseek.com' and url.path.rstrip('/') == '/anthropic'):
        base, mode = 'https://' + url.netloc + '/v1', 'chat_completions'
    return {'inherited': inherited, 'provider': provider, 'model': default_model,
            'route': {'base_url': base, 'api_mode': mode, 'key_cmd': command}}


def _validate(document):
    backend = document['backend_id']
    manifest = CONNECTIONS['read_manifest'](document['manifest_file'])
    kind, _ = CONNECTIONS['resolve_connection'](manifest, backend)
    if 'backend_kind' in document and document['backend_kind'] != kind:
        raise ValueError('connection_kind_changed')
    template_file = Path(manifest['harnesses']['hermes']['config_file'])
    config = read(template_file)
    spec = config['profiles'].get(backend)
    if not isinstance(spec, dict):
        raise ValueError('unknown_backend')
    selected = profile_route(spec, kind)
    inherited, provider, route = selected['inherited'], selected['provider'], selected['route']
    source = Path(config['hermes_source']).resolve()
    if not isinstance(provider, str) or not provider.strip() or provider.strip().lower() in (_provider_ids(source) | {'auto', 'main', 'custom'}):
        raise ValueError('builtin_provider_collision')
    provider = provider.strip()
    default_model = selected['model']
    backend_definition = manifest.get('backends', {}).get(backend, {})
    allowed_models = backend_definition.get('models', [backend_definition.get('model', default_model)])
    if not isinstance(allowed_models, list) or not allowed_models or not all(isinstance(row, str) and row for row in allowed_models):
        raise ValueError('model_mismatch')
    model = document.get('model')
    if not model or model not in allowed_models:
        raise ValueError('model_mismatch')
    helper = document.get('helper_model', 'same')
    if helper != 'same' and helper not in allowed_models:
        raise ValueError('invalid_helper_model')
    resume = document.get('resume_session_id')
    if resume is not None and (not isinstance(resume, str) or not resume.strip()
                               or resume.lower() == 'latest' or resume.startswith('-')):
        raise ValueError('invalid_resume_session_id')
    if document.get('effort') is not None and document['effort'] not in EFFORTS:
        raise ValueError('invalid_effort')
    root = Path(config['managed_root']).resolve()
    install = root / 'installs' / hashlib.sha256(str(source).encode()).hexdigest()[:16]
    environment = Path(read(install / 'facts.json')['packages']['venv']['environment']).resolve()
    executable = environment / 'bin/hermes'
    python = environment / 'bin/python'
    if (not environment.is_relative_to((install / 'environments').resolve())
            or not (environment / 'pyvenv.cfg').is_file()
            or Path(config['hermes_executable']).resolve() != executable.resolve()
            or Path(config['hermes_python']).resolve() != python.resolve()
            or not all(os.access(path, os.X_OK) for path in (executable, python))):
        raise ValueError('invalid_runtime')
    product = Path(document['product_root']).resolve()
    credential_ref = backend_definition.get('credential_ref')
    credential_context = {}
    if 'credential_ref' in backend_definition:
        if route.get('key_cmd'):
            raise ValueError('credential_reference_conflict')
        reader_file = product / 'hooks/llm_secret_reader.py'
        if not reader_file.is_file():
            raise ValueError('credential_reader_unavailable')
        reader = runpy.run_path(str(reader_file))
        reader['validate_ref'](credential_ref)
        reader['validate_environment']()
        route['key_cmd'] = shlex.join(reader['command'](
            product, document['manifest_file'], backend, str(python)))
        credential_context = reader['context']()
    if not isinstance(route.get('key_cmd'), str) or not shlex.split(route['key_cmd']):
        raise ValueError('invalid_route')
    if not (product / 'integrations/hermes/thin_guard.py').is_file():
        raise ValueError('invalid_runtime')
    # Explicit operator context is preserved; no guessed trial defaults/caps.
    context = spec.get('context_length', inherited.get('model', {}).get('context_length'))
    if context is not None:
        if type(context) is not int or context <= 0:
            raise ValueError('invalid_route')
        route['context_length'] = context
    return {'provider': provider, 'model': model, 'helper_model': model if helper == 'same' else helper,
            'route': route, 'source': str(source),
            'root': str(root), 'executable': str(executable), 'python': str(python),
            'profiles_root': str((root / 'profiles').resolve()), 'template_file': str(template_file),
            'credential_ref': credential_ref, 'credential_context': credential_context,
            'credential_files': [str(template_file), str(Path(document['manifest_file']).resolve()),
                                 *([str(Path(spec['template']).resolve())] if spec.get('template') else [])]}


def _save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as handle:
            os.chmod(temporary, 0o600)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _effective_mcp(base, selected):
    """Project the fixed native deep-merge semantics, including None-over-map."""
    if not isinstance(base, dict) or not isinstance(selected, dict):
        return base if isinstance(base, dict) and selected is None else selected
    effective = dict(base)
    for key, value in selected.items():
        effective[key] = _effective_mcp(base.get(key), value)
    return effective


def _link(path, target):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() and path.resolve() == target.resolve():
        return
    if path.exists() or path.is_symlink():
        raise ValueError('owned_link_conflict')
    path.symlink_to(target)


@contextmanager
def _profile_lock(profile):
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (profile / '.orrery-thin.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _auxiliary_tasks(source):
    tree = ast.parse((source / 'hermes_cli/config_defaults.py').read_text())
    defaults = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'DEFAULT_CONFIG' for t in node.targets))
    auxiliary = next(value for key, value in zip(defaults.keys, defaults.values)
                     if isinstance(key, ast.Constant) and key.value == 'auxiliary')
    return [key.value for key, value in zip(auxiliary.keys, auxiliary.values)
            if isinstance(key, ast.Constant) and isinstance(value, (ast.Dict, ast.Call))]


def _helper_route(provider, model):
    # Empty strings are native "unset" values. Omitted leaves would survive
    # the managed scope's deep merge and beat the selected named provider.
    return {'provider': provider, 'model': model, 'base_url': '', 'api': '',
            'api_key': '', 'key_env': '', 'api_key_env': '', 'api_mode': ''}


def _personality_prompt(existing, source):
    """Read native personality data without importing the CLI or executing code."""
    name = str(existing.get('display', {}).get('personality') or '').strip().lower()
    if name in ('', 'none', 'default', 'neutral'):
        return ''
    tree = ast.parse((source / 'hermes_cli/personality.py').read_text())
    builtin = next(node.value for node in tree.body if isinstance(node, ast.AnnAssign)
                   and isinstance(node.target, ast.Name) and node.target.id == 'BUILTIN_PERSONALITIES')
    choices = ast.literal_eval(builtin)
    choices.update(existing.get('personalities') or {})
    choices.update(existing.get('agent', {}).get('personalities') or {})
    value = choices.get(name, '')
    if isinstance(value, dict):
        return '\n'.join(str(item).strip() for item in [value.get('system_prompt', ''),
            'Tone: ' + str(value['tone']) if value.get('tone') else '',
            'Style: ' + str(value['style']) if value.get('style') else ''] if str(item).strip())
    return str(value or '').strip()


def child_home(document, facts=None):
    """Check this identity's persistent HOME before any writes, never in catalog.

    The profiles root may be the installer's generation-external symlink, but
    child namespaces must not redirect into another HOME.
    """
    facts = validate_input(document) if facts is None else facts
    name, project = document.get('agent_name'), document.get('project_key')
    if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', name)
            or not isinstance(project, str) or not project.strip()):
        raise ValueError('invalid_input')
    profiles = Path(facts['profiles_root'])
    profile = profiles / 'children' / hashlib.sha256(project.encode()).hexdigest() / name
    if profile.resolve() != profile:
        raise ValueError('native_resume_profile_changed')
    resume = document.get('resume_session_id') is not None
    pin = document.get('native_profile_env')
    if resume and (pin is None or pin == {}):
        raise ValueError('native_resume_profile_missing')
    if pin is not None:
        if (not isinstance(pin, dict) or set(pin) != {'HERMES_HOME'}
                or not isinstance(pin['HERMES_HOME'], str) or not Path(pin['HERMES_HOME']).is_absolute()):
            raise ValueError('native_resume_profile_changed')
        pinned = Path(pin['HERMES_HOME']).resolve()
        if pinned == profiles / document['backend_id']:
            raise ValueError('native_resume_legacy_profile')
        if pinned != profile:
            raise ValueError('native_resume_profile_changed')
    if resume and (not profile.is_dir() or not os.access(profile, os.R_OK | os.X_OK)):
        raise ValueError('native_resume_home_missing')
    if resume and (not (profile / 'state.db').is_file() or not os.access(profile / 'state.db', os.R_OK)):
        raise ValueError('native_resume_history_missing')
    return profile


def generate(document):
    facts = validate_input(document)
    product, profile = Path(document['product_root']).resolve(), child_home(document, facts)
    state, cwd = Path(document['state_dir']).resolve(), Path(document['cwd']).resolve()
    # A private config slug keeps old named/custom credential pools and endpoint
    # aliases out of this selection. The persistent HOME belongs to one child.
    native_provider = 'orrery-' + document['backend_id'] + '-' + hashlib.sha256(str(state).encode()).hexdigest()[:24]
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', document['agent_name']) or not cwd.is_dir():
        raise ValueError('invalid_input')
    if document.get('worktree') and Path(document['worktree']).resolve() != cwd:
        raise ValueError('invalid_input')
    servers = read(document['mcp_config'])['mcpServers']
    if not isinstance(servers, dict):
        raise ValueError('unsupported_mcp_selection')
    selection = document.get('mcp_selection', {'inherit': True})
    instructions = Path(document['instructions_file']).read_text(encoding='utf-8')
    prompt = Path(document['prompt_file'])
    prompt_text = prompt.read_text(encoding='utf-8')
    with _profile_lock(profile):
        existing = read(profile / 'config.yaml') if (profile / 'config.yaml').exists() else {}
        providers = existing.get('providers')
        providers = providers if isinstance(providers, dict) else {}
        legacy = existing.get('custom_providers')
        legacy = legacy if isinstance(legacy, list) else []
        names = list(providers)
        names += [row.get('name', '') for row in providers.values() if isinstance(row, dict)]
        names += [row.get('name', '') for row in legacy if isinstance(row, dict)]
        if any(str(name).strip().lower().replace(' ', '-').removeprefix('custom:') == native_provider for name in names):
            raise ValueError('native_provider_conflict')
        enabled = existing.get('plugins', {}).get('enabled', [])
        if not isinstance(enabled, list):
            raise ValueError('inherited_mcp_conflict')
        if selection.get('inherit') is False and any(name != PLUGIN for name in enabled):
            # Portable plugins can append MCP servers after config is loaded.
            # No plugin import/preflight or silent expansion of the selection.
            raise ValueError('inherited_mcp_conflict')
        if any(not isinstance(value, dict) for value in servers.values()):
            raise ValueError('unsupported_mcp_selection')
        # Make the native default explicit so a previously disabled server can
        # be selected again. An explicit disabled selection stays disabled.
        mcp = {name: {'enabled': True, **value} for name, value in servers.items()}
        inherited = existing.get('mcp_servers', {})
        if not isinstance(inherited, dict) or any(not isinstance(value, dict) for value in inherited.values()):
            raise ValueError('invalid_mcp_config')
        for name, definition in mcp.items():
            if name in inherited and _effective_mcp(inherited[name], definition) != definition:
                state.mkdir(parents=True, exist_ok=True, mode=0o700)
                with (state / 'config-generation.log').open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps({'event': 'inherited_mcp_conflict', 'mcp_server': name}) + '\n')
                raise ValueError('inherited_mcp_conflict')
        raw_mcp = dict(mcp)
        # Like the standard launcher's strict MCP config, expose only product
        # Mail and explicitly selected servers. Retain disabled definitions for reselection.
        for name in existing.get('mcp_servers', {}):
            if name not in mcp:
                mcp[name] = {'enabled': False}
                raw_mcp[name] = {**inherited[name], 'enabled': False}
        plugin = profile / 'plugins' / PLUGIN
        plugin.mkdir(parents=True, exist_ok=True, mode=0o700)
        manifest = {'name': PLUGIN, 'version': '1.0.0', 'description': 'Common ORRERY native tool guards',
                    'provides_hooks': ['pre_tool_call', 'post_tool_call', 'on_session_start']}
        if (plugin / 'plugin.yaml').exists() and read(plugin / 'plugin.yaml') != manifest:
            raise ValueError('owned_link_conflict')
        _save(plugin / 'plugin.yaml', manifest)
        _link(plugin / '__init__.py', product / 'integrations/hermes/thin_guard.py')
        skills = Path(document.get('skills_root') or product / 'skills').resolve()
        for source in skills.glob('*/SKILL.md'):
            _link(profile / 'skills' / source.parent.name, source.parent)
        auxiliary = existing.get('auxiliary')
        auxiliary = auxiliary if isinstance(auxiliary, dict) else {}
        tasks = list(dict.fromkeys([*_auxiliary_tasks(Path(facts['source'])),
                    *[name for name, value in auxiliary.items() if isinstance(value, dict)]]))
        overlay = {
            'providers': {native_provider: {'api': facts['route']['base_url'], **facts['route']}},
            # The native legacy pool seeder also consults model.api_key/api.
            # Keep those pointers out of this child's route without editing
            # the persistent profile or reading its auth store.
            'model': {'provider': native_provider, 'default': facts['model'],
                      'base_url': facts['route']['base_url'], 'api_mode': facts['route']['api_mode'],
                      'api': '', 'api_key': '', 'key_env': '', 'api_key_env': ''},
            'display': {'busy_input_mode': 'queue'},
            'agent': {'system_prompt': '\n\n'.join(filter(None, [existing.get('agent', {}).get('system_prompt'), instructions]))},
            'auth': {'adopt_external_logins': False},
            'mcp_servers': mcp,
            'plugins': {'enabled': list(dict.fromkeys([*enabled, PLUGIN]))},
            'auxiliary': {task: {**_helper_route(native_provider, facts['helper_model']), 'fallback_chain': []}
                          for task in tasks},
            'delegation': _helper_route(native_provider, facts['helper_model']),
        }
        personality = _personality_prompt(existing, Path(facts['source']))
        # Native consent reads raw config, so the overlay alone cannot settle
        # its offer. Preserve all explicit choices and every other config byte.
        spec = importlib.util.spec_from_file_location('orrery_telemetry_defaults',
            Path(__file__).with_name('telemetry_defaults.py'))
        defaults = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(defaults)
        defaults.settle_unanswered(profile, state)
        defaults.project_mcp(profile, raw_mcp)
        _save(state / 'overlay/config.yaml', overlay)
    definitions = state / 'credential-references.json'
    _save(definitions, {'backends': {'hermes': {
        'auth_command': shlex.split(facts['route']['key_cmd']),
        **({'credential_ref': facts['credential_ref']} if facts['credential_ref'] is not None else {})}}})
    argv = [facts['executable'], 'chat', '--provider', native_provider, '--model', facts['model']]
    if document.get('effort') is not None:
        argv.extend(['--reasoning', document['effort']])
    if document.get('resume_session_id') is not None:
        argv.extend(['--resume', document['resume_session_id']])
    if prompt_text:
        argv.extend(['--query-file', str(prompt)])
    env = {'HERMES_HOME': str(profile), 'HERMES_MANAGED_DIR': str(state / 'overlay'),
           **facts['credential_context'],
           'HERMES_BUNDLED_LOCALES': str(Path(facts['source']) / 'locales'),
           'AGENTSTACK_LLM_KEY_DEFINITIONS': str(definitions),
           'AGENTSTACK_NATIVE_SESSION_FILE': str(state / 'native-session.json'),
           'AGENTSTACK_LLM_KEY_FILES': json.dumps([str(state / 'overlay/config.yaml'),
                str(profile / 'config.yaml'), str(profile / '.env'), str(profile / 'auth.json'),
                *facts['credential_files'], str(definitions)])}
    if personality:
        # Native personality normally wins over agent.system_prompt. Its
        # official ephemeral override preserves it plus the shared block.
        env['HERMES_EPHEMERAL_SYSTEM_PROMPT'] = personality + '\n\n' + instructions
    return {'argv': argv, 'env': env, 'cwd': str(cwd), 'program': 'hermes',
            'native_entry': facts['executable'], 'config_dir': str(state / 'overlay'),
            'profile_home': str(profile), 'guard': {'plugin': str(plugin / '__init__.py'), 'contract': 1}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        _save(args.out, generate(read(args.input)))
    except Exception as error:
        allowed = {'unknown_backend', 'model_mismatch', 'invalid_effort', 'builtin_provider_collision',
                   'invalid_connection_kind', 'connection_kind_changed', 'ambiguous_connection_configuration',
                   'invalid_runtime', 'invalid_route', 'invalid_input', 'invalid_telemetry_config', 'invalid_mcp_config', 'unsupported_mcp_selection',
                   'inherited_mcp_conflict', 'owned_link_conflict', 'invalid_helper_model',
                   'invalid_resume_session_id', 'native_resume_profile_changed', 'native_provider_conflict',
                   'native_resume_profile_missing', 'native_resume_legacy_profile', 'native_resume_home_missing',
                   'native_resume_history_missing',
                   'invalid_credential_ref', 'credential_reference_conflict',
                   'unsupported_credential_store', 'credential_reader_unavailable'}
        reason = str(error) if str(error) in allowed else 'invalid_input'
        print('HERMES_THIN_CONFIG_ERROR=' + reason, file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
