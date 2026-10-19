"""Hermes projection of the product lifecycle, matching the dsh state contract.

Registration is an explicit operator step. Never read a token value, retry a
partial registration automatically, or alter the generated proxy environment.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess


def stamp():
    return datetime.now(timezone.utc).isoformat()


def save(path, value, *, exclusive=False):
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    with os.fdopen(os.open(path, flags, 0o600), 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def read(path):
    return json.loads(Path(path).read_text())


def product_env(config):
    product, connection = Path(config['product_root']), config['connection']
    search = [str(Path(config['proxy_python']).parent), str(Path(config['tmux']).parent), '/usr/bin', '/bin']
    return {'HOME': config['home'], 'PATH': os.pathsep.join(dict.fromkeys(search)),
            'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8',
            'AGENTSTACK_HOME': str(product), 'AGENTSTACK_PROJECT_KEY': config['project_key'],
            'PROJECT_KEY': config['project_key'], 'AGENTSTACK_PYTHON': config['proxy_python'],
            'AGENTSTACK_RUNTIME_DIR': connection['runtime_dir'], 'AGENTSTACK_MCP_URL': connection['mcp_url'],
            'AGENTSTACK_MAIL_ENV': connection['mail_env'],
            'AGENTSTACK_MAIL_HTTP_BEARER_MODE': connection['bearer_mode']}


def require_legacy(config, run=subprocess.run):
    result = run([str(Path(config['product_root']) / 'bin/agentstack-runtime-client'), 'mode'],
                 env=product_env(config), capture_output=True, text=True, check=True, timeout=20)
    if result.stdout.strip() != 'legacy':
        raise ValueError('Selected product mode is not the inspected legacy route')


def register(state, run=subprocess.run, *, generate_config=True):
    state = Path(state)
    config, attempt = read(state / 'run-config.json'), read(state / 'attempt.json')
    if attempt['phase'] != 'run':
        raise ValueError('Registration requires an explicitly prepared run attempt')
    require_legacy(config, run)
    product = Path(config['product_root'])
    helper, generator = product / 'bin/agentstack-preregister-child', product / 'hooks/child_tools.py'
    runner = product / 'integrations/codex_app/plugin/scripts/run-mcp.sh'
    if not helper.is_file() or not generator.is_file() or not os.access(runner, os.X_OK):
        raise ValueError('Canonical product helper/generator/proxy unavailable')
    if (state / 'mail-identity.json').exists():
        raise ValueError('Identity already exists; do not register another')
    save(state / 'registration-intent.json', {'at': stamp(), 'status': 'started'}, exclusive=True)
    pending, env = state / 'owner.pending', product_env(config)
    result = run([str(helper), '--project-key', config['project_key'], '--program', 'hermes',
                  '--model', config['model'], '--task-description', 'One managed Hermes task',
                  '--token-file-out', str(pending)], env=env, cwd=state,
                 capture_output=True, text=True, timeout=60)
    save(state / 'registration-result.json', {'exit_code': result.returncode, 'at': stamp()})
    if result.returncode:
        raise ValueError('Preregistration failed; preserve intent and inspect the same identity')
    name = result.stdout.strip()
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]+', name) or name == config['parent']:
        raise ValueError('Unexpected helper-returned identity; preserve intent')
    token = state / ('token-' + name)
    pending.rename(token)  # Product handoff only; never open its contents.
    if token.stat().st_mode & 0o777 != 0o600:
        raise ValueError('Product handoff must be 0600')
    identity = {'agent_name': name, 'program': 'hermes', 'parent': config['parent'],
                'project_key': config['project_key'], 'registered_at': stamp(), 'token_file': str(token)}
    save(state / 'mail-identity.json', identity, exclusive=True)
    attempt['agent_name'] = name
    save(state / 'attempt.json', attempt)
    if generate_config:
        generate(state, run)
    return name


def generate(state, run=subprocess.run):
    state = Path(state)
    config, identity = read(state / 'run-config.json'), read(state / 'mail-identity.json')
    require_legacy(config, run)
    product, connection = Path(config['product_root']), config['connection']
    generator = product / 'hooks/child_tools.py'
    runner = product / 'integrations/codex_app/plugin/scripts/run-mcp.sh'
    name, token, env = identity['agent_name'], Path(identity['token_file']), product_env(config)
    if identity['program'] != 'hermes' or identity['project_key'] != config['project_key']:
        raise ValueError('Generated config identity mismatch')
    if (state / 'mcp.json').exists():
        raise ValueError('Generated config already exists; preserve the previous step')
    empty = state / 'empty-claude.json'
    save(empty, {}, exclusive=True)
    run([config['proxy_python'], str(generator), 'claude-config', '--spec',
         '{"base":"mail-only","tools":{}}', '--cwd', config['worktree'], '--out', str(state / 'mcp.json'),
         '--runner', str(runner), '--child', name, '--project-key', config['project_key'],
         '--token-file', str(token), '--mcp-url', connection['mcp_url'], '--mail-env', connection['mail_env'],
         '--runtime-dir', connection['runtime_dir'], '--bearer-mode', connection['bearer_mode'],
         '--python-bin', config['proxy_python'], '--parent-agent', config['parent'],
         '--claude-json', str(empty), '--chrome', '0'], env=env, cwd=state,
        capture_output=True, text=True, check=True, timeout=30)
    proxy = read(state / 'mcp.json')['mcpServers']['orrery-mail']
    if proxy['command'] != str(runner) or proxy.get('args') != []:
        raise ValueError('Unexpected generated product proxy command')
    if any(key in proxy['env'] for key in ('DEEPSEEK_API_KEY', 'CHILD_REGISTRATION_TOKEN', 'MCP_AGENT_MAIL_TOKEN')):
        raise ValueError('Generated proxy contains a credential value field')
    # Preserve the product's PROGRAM too: remote whois checks program=hermes.
    return proxy


def retire(state, run=subprocess.run, *, require_release=True):
    state = Path(state)
    config, attempt, identity = [read(state / name) for name in
                                 ('run-config.json', 'attempt.json', 'mail-identity.json')]
    if attempt['agent_name'] != identity['agent_name']:
        raise ValueError('Retire identity mismatch')
    exit_receipt = read(state / 'runtime-exit.json')
    if exit_receipt.get('group_absent') is not True or exit_receipt.get('group_check') != 'observed':
        raise ValueError('Owned group absence must be confirmed before retirement')
    if require_release:
        release = read(state / 'release-receipt.json')
        if (release.get('attempt_id') != attempt['attempt_id'] or release.get('agent_name') != identity['agent_name']
                or release.get('confirmed') is not True):
            raise ValueError('Real reservation release not confirmed')
    elif config.get('purpose') != 'mail-connectivity' or (state / 'reservations.json').exists():
        raise ValueError('Only the reservation-free Mail pilot can skip release')
    require_legacy(config, run)
    env = product_env(config)
    env['AGENT_NAME'] = identity['agent_name']
    result = run(['/bin/bash', str(Path(config['product_root']) / 'hooks/cleanup-child-agent.sh'),
                  identity['agent_name']], env=env, cwd=state, capture_output=True, timeout=60)
    receipt = {'attempt_id': attempt['attempt_id'], 'agent_name': identity['agent_name'],
               'cleanup_exit_code': result.returncode, 'retired_at': None, 'confirmed': False,
               'source': 'parent-bound-whois', 'checked_at': None}
    save(state / 'retire-receipt.json', receipt)
    return receipt  # Only the parent's canonical whois can confirm retirement.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['register', 'retire'])
    parser.add_argument('--state', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = register(args.state) if args.action == 'register' else retire(args.state)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError):
        raise SystemExit('Lifecycle step failed; preserve state, no automatic retry')
    print(json.dumps({'action': args.action, 'result': result}))


if __name__ == '__main__':
    main()
