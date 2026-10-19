"""Read a route template and emit a private Hermes profile without copying state.

Run with Hermes's installed dependency interpreter. Only command references,
never a literal API key or another profile's memory/auth/env, are copied.
"""
import argparse
import json
from pathlib import Path
import sys


def load_template(path):
    path = Path(path)
    if path.suffix == '.json':
        return json.loads(path.read_text())
    import hermes_yaml
    return hermes_yaml.safe_load(path.read_text())


def project(spec, profile, auxiliary_tasks):
    template = load_template(spec['template']) if spec.get('template') else {}
    model_config = template.get('model', {})
    provider = spec.get('provider') or model_config.get('provider')
    entry = template.get('providers', {}).get(provider, {})
    if not entry:
        entry = next((row for row in template.get('custom_providers', []) if row.get('name') == provider), {})
    route = {key: entry[key] for key in ('base_url', 'api', 'api_mode', 'transport', 'model', 'key_cmd') if key in entry}
    route.update({key: spec[key] for key in ('base_url', 'api', 'api_mode', 'transport', 'model', 'key_cmd') if key in spec})
    model = spec.get('model') or model_config.get('default') or route.get('model')
    base = route.get('base_url') or route.get('api')
    mode = route.get('api_mode') or route.get('transport')
    if (not isinstance(provider, str) or provider in ('', 'auto', 'main')
            or not isinstance(model, str) or not model or not isinstance(base, str) or not base.startswith('https://')
            or mode not in ('chat_completions', 'anthropic_messages')
            or not isinstance(route.get('key_cmd'), str) or not route['key_cmd'].strip()):
        raise ValueError('Explicit provider/model/HTTPS route/transport/key command reference required')
    # Canonical route spelling: aux inherits this named entry's key_cmd rather
    # than losing it by flattening into an unnamed custom endpoint.
    route = {'base_url': base, 'api_mode': mode, 'model': model,
             'key_cmd': route['key_cmd'], 'discover_models': False}
    tasks = set(auxiliary_tasks) | {key for key, value in template.get('auxiliary', {}).items() if isinstance(value, dict)}
    auxiliary = {task: {'provider': provider, 'model': model, 'api_mode': mode, 'fallback_chain': []}
                 for task in sorted(tasks)}
    auxiliary['title_generation'].update(enabled=True, model_upgrade_enabled=True)
    auxiliary['background_review']['enabled'] = True
    profile = Path(profile).resolve()
    config = {'model': {'provider': provider, 'default': model}, 'providers': {provider: route},
              'fallback_providers': [], 'auth': {'adopt_external_logins': False},
              'updates': {'check': False}, 'model_catalog': {'enabled': False},
              'telemetry': {'shared_metrics': {'enabled': False, 'send': False}},
              'security': {'allow_lazy_installs': False},
              'auxiliary': auxiliary,
              'memory': {'memory_enabled': True, 'user_profile_enabled': True, 'provider': ''},
              'skills': {'external_dirs': [], 'create_dir': str(profile / 'skills'), 'trusted_project_dirs': []},
              'plugins': {'enabled': ['orrery-mail-receipt']},
              'hooks': {},
              'delegation': {'fallback_providers': []}}
    context = spec.get('context_length', model_config.get('context_length'))
    if context is not None:
        if type(context) is not int or context <= 0:
            raise ValueError('Context length must be an explicit positive integer')
        config['model']['context_length'] = context
        # Named endpoints resolve to provider="custom" at runtime. The route
        # pin survives that normalization and callers without a provider name.
        route['context_length'] = context
        # Aux context checks omit config_context_length. The exact override is
        # consulted before live probes in those checks as well as the main one.
        config['model_overrides'] = {provider: {model: {'context_window': context}}}
        if 'compression' in auxiliary:
            auxiliary['compression']['context_length'] = context
    effort = spec.get('reasoning_effort', template.get('agent', {}).get('reasoning_effort'))
    if effort not in (None, ''):
        if effort not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
            raise ValueError('Unknown explicit reasoning effort')
        config['agent'] = {'reasoning_effort': effort}
    return {'provider': provider, 'model': model, 'profile_config': config}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', required=True, type=Path)
    parser.add_argument('--profile', required=True, type=Path)
    parser.add_argument('--source', required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source))
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    tasks = [key for key, value in DEFAULT_CONFIG['auxiliary'].items() if isinstance(value, dict)]
    print(json.dumps(project(json.loads(args.spec.read_text()), args.profile, tasks)))


if __name__ == '__main__':
    main()
