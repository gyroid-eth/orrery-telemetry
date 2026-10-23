"""Write an ordinary native dsh profile. No keys, provider imports or requests.

Ordinary config mistakes can launch the wrong route or expose credentials;
validate references and representability before writing the launch plan.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import sys
from urllib.parse import urlsplit

THIN_CONFIG_CONTRACT = 1
HELPER_MODEL_CONTRACT = 1
CONNECTIONS = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'hooks/native_connections.py'))


def read(file):
    return CONNECTIONS['read_manifest'](file)


def save(file, value):
    file = Path(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(value, indent=2) + '\n')
    file.chmod(0o600)


def absolute(value, *, directory=False):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError('invalid_path')
    path = Path(value)
    if not (path.is_dir() if directory else path.is_file()):
        raise ValueError('missing_file')
    return path


def model_ids(metadata):
    values = metadata.get('models', [metadata.get('model')])
    if not isinstance(values, list):
        raise ValueError('model_mismatch')
    return [v if isinstance(v, str) else v.get('id') for v in values if isinstance(v, (str, dict))]


def mcp_rows(document):
    if 'mcp_config' not in document:
        return []
    selection = document.get('mcp_selection', {})
    if selection.get('approved') or selection.get('enabled_tools'):
        raise ValueError('unsupported_mcp_selection')
    servers = read(absolute(document['mcp_config'])).get('mcpServers')
    if not isinstance(servers, dict):
        raise ValueError('unsupported_mcp_selection')
    rows = []
    for name, value in servers.items():
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,32}', name) or not isinstance(value, dict):
            raise ValueError('unsupported_mcp_selection')
        if value.get('disabled') is True:
            continue
        config = {'serverName': name}
        if value.get('command') and value.get('type', 'stdio') == 'stdio':
            config.update(transport='stdio', command=value['command'], args=value.get('args', []),
                          env=value.get('env', {}), cwd=value.get('cwd', document.get('cwd', '')))
        elif value.get('type') in ('http', 'streamable-http') and value.get('url'):
            config.update(transport='streamable-http', url=value['url'], headers=value.get('headers', {}))
        else:
            raise ValueError('unsupported_mcp_selection')
        # Never silently discard tool filters or approval requirements.
        if set(value) - {'command', 'args', 'env', 'cwd', 'type', 'url', 'headers', 'disabled'}:
            raise ValueError('unsupported_mcp_selection')
        rows.append({'id': 'orrery-mcp-' + name, 'name': '@deepseek-ai/dsh-mcp-client', 'config': config})
    return rows


def validate_input(document):
    """Read-only admission; callable with the shared selection subset."""
    try:
        root = absolute(document['product_root'], directory=True)
        manifest = CONNECTIONS['read_manifest'](absolute(document['manifest_file']))
        backend_id = document['backend_id']
        kind, metadata = CONNECTIONS['resolve_connection'](manifest, backend_id)
        if 'backend_kind' in document and document['backend_kind'] != kind:
            raise ValueError('connection_kind_changed')
        if document['model'] not in model_ids(metadata):
            raise ValueError('model_mismatch')
        helper = document.get('helper_model', 'same')
        if helper is None:
            helper = 'same'
        if helper != 'same' and helper not in model_ids(metadata):
            raise ValueError('invalid_helper_model')
        template = read(absolute(manifest['harnesses']['dsh']['config_file']))
        owner = read(absolute(template['generation_receipt']))
        if (owner.get('variant') != 'plain-datadir' or owner.get('thin_config_contract') != 1
                or owner.get('harness') != 'dsh' or owner.get('complete') is not True
                or template.get('patch_receipt')):
            raise ValueError('plain_runtime_required')
        details = owner['details']
        node = absolute(template['node'])
        runtime = absolute(template['runtime'], directory=True)
        if str(node) != details['node'] or str(runtime) != details['runtime'] or not os.access(node, os.X_OK):
            raise ValueError('invalid_runtime')
        proof = read(runtime.parent / 'evidence/node-runtime.json')
        if hashlib.sha256(node.read_bytes()).hexdigest() != proof['sha256']:
            raise ValueError('invalid_runtime')
        version = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)', proof.get('version', ''))
        if not version or tuple(map(int, version.groups())) < (24, 2, 0) or Path(proof['execPath']).resolve() != node.resolve():
            raise ValueError('invalid_runtime')
        for pin in read(root / 'integrations/dsh/runtime-pins.json'):
            if read(runtime / 'node_modules' / pin['name'] / 'package.json')['version'] != pin['version']:
                raise ValueError('runtime_version_mismatch')
        entry = absolute(str(runtime / 'node_modules/@deepseek-ai/dsh/lib/bin.js'))
        source = read(details['runtime_receipt'])
        if source.get('variant') != 'plain-datadir' or source.get('source_patch') != 'tui-datadir-v1':
            raise ValueError('plain_runtime_required')
        if source.get('recipe_sha256') != hashlib.sha256(
                (root / 'integrations/dsh/tui-datadir.patch.json').read_bytes()).hexdigest():
            raise ValueError('invalid_runtime')
        for row in source['files']:
            file = runtime / 'node_modules/@deepseek-harness-tui/dsh-tui' / row['file']
            if hashlib.sha256(file.read_bytes()).hexdigest() != row['sha256']:
                raise ValueError('invalid_runtime')
        backend_file = absolute(template['backend_file'])
        backend = read(backend_file)['backends'][backend_id]
        url = urlsplit(backend['base_url'])
        if url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password:
            raise ValueError('invalid_backend_url')
        effort = document.get('effort')
        if effort is not None and effort not in ('off', 'low', 'high', 'max'):
            raise ValueError('invalid_effort')
        plugin = root / 'integrations/dsh/orrery_tools.mjs'
        credential_plugin = root / 'integrations/dsh/keychain_credentials.mjs'
        if not plugin.is_file():
            raise ValueError('guard_unavailable')
        if not credential_plugin.is_file():
            raise ValueError('credential_adapter_unavailable')
        credential_ref = metadata.get('credential_ref')
        reader_command = None
        credential_context = {}
        if 'credential_ref' in metadata:
            if backend.get('keychain') is not None:
                raise ValueError('credential_reference_conflict')
            reader_file = root / 'hooks/llm_secret_reader.py'
            if not reader_file.is_file():
                raise ValueError('credential_reader_unavailable')
            reader = runpy.run_path(str(reader_file))
            reader['validate_ref'](credential_ref)
            reader['validate_environment']()
            reader_command = reader['command'](root, document['manifest_file'], backend_id, sys.executable)
            credential_context = reader['context']()
        else:
            selector = backend['keychain']
            if not all(isinstance(selector.get(k), str) and selector[k] for k in ('service', 'account')):
                raise ValueError('invalid_credential_selector')
        rows = mcp_rows(document)
        return dict(root=root, template=template, backend=backend, backend_file=backend_file,
                    node=node, runtime=runtime, entry=entry, plugin=plugin,
                    credential_plugin=credential_plugin, helper=helper, mcp_rows=rows,
                    credential_ref=credential_ref, reader_command=reader_command,
                    credential_context=credential_context)
    except (KeyError, TypeError, OSError, json.JSONDecodeError):
        raise ValueError('invalid_configuration') from None


def link(source, target):
    source, target = Path(source), Path(target)
    if target.is_symlink() and target.resolve() == source.resolve():
        return
    if target.exists() or target.is_symlink():
        raise ValueError('owned_link_conflict')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(source, target_is_directory=source.is_dir())


def generate(document):
    facts = validate_input(document)
    name = document['agent_name']
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', name):
        raise ValueError('invalid_agent_name')
    cwd = absolute(document['cwd'], directory=True)
    state = absolute(document['state_dir'], directory=True)
    home = absolute(document['runtime_dir'], directory=True) / 'native/dsh' / name
    # Resume after a changed runtime root is ordinary configuration drift;
    # reject before writing into a different persistent native profile.
    if 'native_profile_env' in document and document['native_profile_env'] != {'DSH_HOME': str(home)}:
        raise ValueError('native_resume_profile_changed')
    # Home-level native patches are inherited after profile patches. A
    # mail-only selection cannot promise to suppress unknown plugin clients.
    inherited = home / 'cordis.patch.yml'
    if document.get('mcp_selection', {}).get('inherit') is False and inherited.exists():
        if inherited.read_text().strip() not in ('', '[]'):
            raise ValueError('inherited_mcp_conflict')
    config_dir = state / 'dsh-config'
    profile = home / 'profiles/orrery'
    profile.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    profile.chmod(0o700)
    save(profile / 'package.json', {'name': 'orrery-native-dsh', 'private': True,
        'dsh': {'profile': {'bundles': ['@deepseek-ai/dsh-base', '@deepseek-harness-tui/dsh-tui']}}})
    link(facts['runtime'] / 'node_modules', profile / 'node_modules')
    # An owned explicit profile has no user/plugin MCP client rows. dsh-base's
    # mcp-resources row consumes existing clients; it does not discover servers.
    save(profile / 'cordis.patch.yml', [])
    shutil.copyfile(absolute(document['instructions_file']), home / 'AGENTS.md')
    (home / 'AGENTS.md').chmod(0o600)
    skill_roots = document.get('skill_roots', [])
    if not isinstance(skill_roots, list):
        raise ValueError('invalid_skill_roots')
    for skill in skill_roots:
        absolute(skill, directory=True)
    skills = facts['root'] / 'skills'
    if skills.is_dir():
        # Native scanner walks one level <name>/SKILL.md: link each skill.
        for skill in skills.iterdir():
            if skill.is_dir() and (skill / 'SKILL.md').is_file():
                link(skill, home / 'skills' / skill.name)
    backend = facts['backend']
    ref = 'ORRERY_DSH_PROVIDER_KEY'
    provider = {'apiKeyEnv': ref, 'baseURL': backend['base_url']}
    if backend.get('model_definition'):
        provider['models'] = [backend['model_definition']]
    route = {'provider': 'deepseek-official', 'model': document['model']}
    tui = {**route, 'cwd': str(cwd)}
    if document.get('effort') is not None:
        route['reasoningEffort'] = document['effort']
        tui['effort'] = document['effort']
    guard_config = {'cwd': str(cwd), 'state_dir': str(state), 'hooks_dir': str(facts['root'] / 'hooks'),
                    'env': document.get('env', {})}
    # In native patches, `name` is a match predicate, not a replacement.
    # Disable the bundled file/env source and insert our credentials service.
    rows = [
        {'id': 'credentials', 'disabled': True},
        {'id': 'working-activity', 'disabled': True},
        {'id': 'llm-deepseek', 'inject': ['llm', 'credentials'], 'config': provider},
        {'id': 'agent-default-model', 'config': route},
        {'id': 'dsh-tui', 'config': tui},
        {'insert': [
            {'id': 'orrery-keychain-credentials', 'name': str(facts['credential_plugin']),
             'config': {'ref': ref, 'runtime': str(facts['runtime']), 'dshHome': str(home),
                        **({'reader_command': facts['reader_command']} if facts['reader_command']
                           else backend['keychain'])}},
            {'id': 'orrery-tools', 'name': str(facts['plugin']), 'config': guard_config}, *facts['mcp_rows']]},
    ]
    if skill_roots:
        rows.append({'id': 'skill-filesystem', 'config': {'customSkillDirs': skill_roots}})
    if facts['helper'] != 'same':
        rows.append({'id': 'tool-subagent', 'config': {'agentOptions': {
            'provider': 'deepseek-official', 'model': facts['helper']}}})
    tui_data = state / 'tui'
    save(tui_data / 'model.json', {'provider': 'deepseek-official', 'model': document['model']})
    language = 'en'
    save(tui_data / 'lang.json', {'lang': language})
    save(tui_data / 'onboarding.json', {'completed': True, 'version': 1})
    overlay = config_dir / 'native.patch.json'
    save(overlay, rows)
    argv = [str(facts['node']), str(facts['entry']), '--profile', 'orrery', '--patch', str(overlay)]
    if 'resume_session_id' in document:
        session = document['resume_session_id']
        if not isinstance(session, str) or not session or session.startswith('-'):
            raise ValueError('invalid_resume_session_id')
        argv += ['--resume', session]
    prompt = absolute(document['prompt_file']).read_text()
    if prompt:
        argv += ['--', prompt]
    protected = [str(overlay), str(home / '.credentials.yaml'), str(facts['backend_file'])]
    definitions = facts['backend_file']
    if facts['reader_command']:
        definitions = state / 'credential-references.json'
        save(definitions, {'backends': {document['backend_id']: {
            'credential_ref': facts['credential_ref'], 'auth_command': facts['reader_command']}}})
        protected.extend([str(definitions), str(Path(document['manifest_file']).resolve())])
    return {'argv': argv, 'cwd': str(cwd), 'config_dir': str(config_dir),
            'env': {'DSH_HOME': str(home), 'DSH_TUI_DATA_DIR': str(tui_data), 'DSH_TUI_LANG': language,
                    **facts['credential_context'],
                    'AGENTSTACK_LLM_KEY_FILES': json.dumps(protected),
                    'AGENTSTACK_LLM_KEY_DEFINITIONS': str(definitions)},
            'guard': {'plugin': str(facts['plugin']), 'contract': 1}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    try:
        save(args.out, generate(read(args.input)))
        return 0
    except (ValueError, OSError, TypeError, KeyError) as error:
        reason = str(error) if type(error) is ValueError and re.fullmatch('[a-z_]+', str(error)) else 'invalid_configuration'
        print('DSH_THIN_CONFIG_ERROR=' + reason, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
