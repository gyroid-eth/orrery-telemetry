"""Product lifecycle adapter. No token values or model credentials are read.

Registration failures happen normally; keep one registration intent so a
partial failure cannot silently create a second live identity.
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
    # Only non-secret product connection references, never inherited credentials.
    product = Path(config['product_root'])
    c = config['connection']
    return {'HOME': config['home'], 'PATH': str(Path(config['node']).parent) + ':/usr/bin:/bin',
            'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8',
            'AGENTSTACK_HOME': str(product), 'AGENTSTACK_PROJECT_KEY': config['project_key'],
            'PROJECT_KEY': config['project_key'], 'AGENTSTACK_PYTHON': config['proxy_python'],
            'AGENTSTACK_RUNTIME_DIR': c['runtime_dir'], 'AGENTSTACK_MCP_URL': c['mcp_url'],
            'AGENTSTACK_MAIL_ENV': c['mail_env'], 'AGENTSTACK_MAIL_HTTP_BEARER_MODE': c['bearer_mode']}


def require_legacy(config, run=subprocess.run):
    result = run([str(Path(config['product_root']) / 'bin/agentstack-runtime-client'), 'mode'],
                 env=product_env(config), capture_output=True, text=True, check=True, timeout=20)
    if result.stdout.strip() != 'legacy':
        raise ValueError('Selected product mode is not the inspected legacy route')


def register(state, run=subprocess.run):
    state = Path(state)
    config = read(state / 'run-config.json')
    require_legacy(config, run)
    product = Path(config['product_root'])
    helper = product / 'bin/agentstack-preregister-child'
    generator = product / 'hooks/child_tools.py'
    runner = product / 'integrations/codex_app/plugin/scripts/run-mcp.sh'
    if not helper.is_file() or not generator.is_file() or not os.access(runner, os.X_OK):
        raise ValueError('Canonical product helper/generator/proxy unavailable')
    if (state / 'mail-identity.json').exists():
        raise ValueError('Identity already exists; do not register another')
    save(state / 'registration-intent.json', {'at': stamp(), 'status': 'started'}, exclusive=True)
    pending = state / 'owner.pending'
    env = product_env(config)
    result = run([str(helper), '--project-key', config['project_key'], '--program', 'dsh',
                  '--model', config['model'], '--task-description', 'One managed dsh TUI task',
                  '--token-file-out', str(pending)], env=env, cwd=state,
                 capture_output=True, text=True, timeout=60)
    save(state / 'registration-result.json', {'exit_code': result.returncode, 'at': stamp()})
    if result.returncode:
        raise ValueError('Preregistration failed; preserve intent and inspect the same identity')
    name = result.stdout.strip()
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]+', name) or name == config['parent']:
        raise ValueError('Unexpected helper-returned identity; preserve intent')
    token = state / ('token-' + name)
    pending.rename(token)  # Product-owned handoff: never open its contents.
    if token.stat().st_mode & 0o777 != 0o600:
        raise ValueError('Product handoff must be 0600')
    identity = {'agent_name': name, 'program': 'dsh', 'parent': config['parent'],
                'project_key': config['project_key'], 'registered_at': stamp(),
                'token_file': str(token)}
    save(state / 'mail-identity.json', identity, exclusive=True)
    empty = state / 'empty-claude.json'
    save(empty, {}, exclusive=True)
    connection = config['connection']
    run([config['proxy_python'], str(generator), 'claude-config', '--spec',
         '{"base":"mail-only","tools":{}}', '--cwd', config['worktree'], '--out', str(state / 'mcp.json'),
         '--runner', str(runner), '--child', name, '--project-key', config['project_key'],
         '--token-file', str(token), '--mcp-url', connection['mcp_url'],
         '--mail-env', connection['mail_env'], '--runtime-dir', connection['runtime_dir'],
         '--bearer-mode', connection['bearer_mode'], '--python-bin', config['proxy_python'],
         '--parent-agent', config['parent'], '--claude-json', str(empty), '--chrome', '0'],
        env=env, cwd=state, capture_output=True, text=True, check=True, timeout=30)
    proxy = read(state / 'mcp.json')['mcpServers']['orrery-mail']
    if proxy['command'] != str(runner) or proxy.get('args') != []:
        raise ValueError('Unexpected generated product proxy command')
    # Keep command/args/env exactly as generated, including the display program.
    if any(k in proxy['env'] for k in ('DEEPSEEK_API_KEY', 'CHILD_REGISTRATION_TOKEN', 'MCP_AGENT_MAIL_TOKEN')):
        raise ValueError('Generated proxy config contains a credential value field')
    patch = [{'insert': [{'id': 'orrery-mail', 'name': '@deepseek-ai/dsh-mcp-client',
              'config': {'serverName': 'orrery_mail', 'transport': 'stdio', **proxy,
                         'cwd': config['worktree'], 'failOnStartupError': True,
                         'reconnect': {'enabled': False}}}]}]
    save(state / 'mail.patch.json', patch, exclusive=True)
    return name


def retire(state, run=subprocess.run):
    state = Path(state)
    config, attempt, identity = [read(state / f) for f in
                                 ('run-config.json', 'attempt.json', 'mail-identity.json')]
    if attempt['agent_name'] != identity['agent_name']:
        raise ValueError('Retire identity mismatch')
    exit_receipt = read(state / 'runtime-exit.json')
    if exit_receipt.get('group_absent') is not True or exit_receipt.get('group_check') != 'observed':
        raise ValueError('Owned group absence must be confirmed before retirement')
    release_file = state / 'release-receipt.json'
    if release_file.exists():
        release = read(release_file)
        if release.get('attempt_id') != attempt['attempt_id'] or release.get('agent_name') != identity['agent_name'] or release.get('confirmed') is not True or release.get('source') != 'product-bound-proxy':
            raise ValueError('Real reservation release not confirmed')
    elif (state / 'reservation-intent.json').exists() or exit_receipt.get('runtime_not_started') is not True:
        raise ValueError('Reservation attempt needs confirmed release before cleanup')
    require_legacy(config, run)
    env = product_env(config)
    env['AGENT_NAME'] = identity['agent_name']
    hook = Path(config['product_root']) / 'hooks/cleanup-child-agent.sh'
    result = run(['/bin/bash', str(hook), identity['agent_name']], env=env, cwd=state,
                 capture_output=True, timeout=60)
    receipt = {'attempt_id': attempt['attempt_id'], 'agent_name': identity['agent_name'],
               'cleanup_exit_code': result.returncode, 'retired_at': None, 'confirmed': False,
               'source': 'parent-bound-whois', 'checked_at': None}
    save(state / 'retire-receipt.json', receipt)
    # Product cleanup suppresses some RPC errors. Parent's canonical whois must
    # confirm the same name and record retired_at before any credential clean.
    return receipt


def capture_value(value):
    """Read an operator-saved MCP result; never make an RPC here."""
    if isinstance(value, dict) and value.get('isError'):
        raise ValueError('Parent MCP call failed')
    if isinstance(value, dict) and 'structuredContent' in value:
        value = value['structuredContent']
    elif isinstance(value, dict) and isinstance(value.get('content'), list):
        blocks = [block for block in value['content'] if block.get('type') == 'text']
        if len(blocks) != 1:
            raise ValueError('Expected one JSON MCP text result')
        value = json.loads(blocks[0]['text'])
    return value.get('result', value) if isinstance(value, dict) else value


def record_mail(state, input_file):
    state = Path(state)
    attempt, task = read(state / 'attempt.json'), read(state / 'task.json')
    parent = read(state / 'run-config.json')['parent']
    if task['attempt_id'] != attempt['attempt_id'] or task['report_to'] != parent:
        raise ValueError('Task attempt mismatch')
    value = capture_value(read(input_file))
    messages = value if isinstance(value, list) else [value]
    events = [json.loads(line) for line in (state / 'events.jsonl').read_text().splitlines()]
    receipts = [e for e in events if e.get('phase') == 'mail-sent' and
                e.get('attempt_id') == attempt['attempt_id'] and e.get('task_id') == task['task_id'] and
                e.get('from') == attempt['agent_name'] and e.get('to') == parent and
                e.get('verified_sender') is True and e.get('ok') is True]
    matches = [m for m in messages if isinstance(m, dict) and type(m.get('id')) is int and m['id'] > 0 and
               m.get('from') == attempt['agent_name'] and m.get('kind') == 'to' and 'to' not in m and
               any(e.get('message_id') == m['id'] for e in receipts)]
    if len(matches) != 1:
        raise ValueError('Parent inbox result must match this attempt\'s actual child Mail receipt')
    message = matches[0]
    receipt = {'attempt_id': attempt['attempt_id'], 'task_id': task['task_id'],
               'agent_name': attempt['agent_name'], 'message_id': message['id'],
               'from': message['from'], 'to': [parent],
               'source': 'parent-bound-fetch-inbox', 'checked_at': stamp()}
    save(state / 'parent-mail-receipt.json', receipt)
    return receipt


def confirm_retire(state, input_file):
    state = Path(state)
    attempt = read(state / 'attempt.json')
    receipt = read(state / 'retire-receipt.json')
    who = capture_value(read(input_file))
    if not isinstance(who, dict) or who.get('name') != attempt['agent_name'] or not isinstance(who.get('retired_at'), str) or not who['retired_at'].strip():
        raise ValueError('Canonical parent whois did not confirm retirement of this child')
    # Preserve the real cleanup exit status, separately from remote retirement.
    if receipt.get('attempt_id') != attempt['attempt_id'] or receipt.get('agent_name') != attempt['agent_name'] or type(receipt.get('cleanup_exit_code')) is not int:
        raise ValueError('Cleanup receipt is missing or belongs to another attempt')
    datetime.fromisoformat(who['retired_at'].replace('Z', '+00:00'))
    receipt.update(retired_at=who['retired_at'], confirmed=True,
                   source='parent-bound-whois', checked_at=stamp())
    save(state / 'retire-receipt.json', receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['register', 'retire', 'record-mail', 'confirm-retire'])
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--input', type=Path)
    args = parser.parse_args()
    try:
        if args.action in ('record-mail', 'confirm-retire') and not args.input:
            raise ValueError('Parent-saved canonical MCP result file required')
        if args.action == 'register':
            result = register(args.state)
        elif args.action == 'retire':
            result = retire(args.state)
        elif args.action == 'record-mail':
            result = record_mail(args.state, args.input)
        else:
            result = confirm_retire(args.state, args.input)
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        raise SystemExit('Lifecycle step failed; preserve state, no automatic retry')
    print(json.dumps({'action': args.action, 'result': result}))


if __name__ == '__main__':
    main()
