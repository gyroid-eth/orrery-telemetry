"""Official SDK -> unchanged product stdio proxy; schema is not authentication."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mail_lifecycle import read, save, stamp


REQUIRED = {'runtime_status', 'whois', 'send_message', 'reserve_files', 'release_reservations'}


def field(value, snake, camel=None, default=None):
    return getattr(value, snake, getattr(value, camel or snake, default))


def unwrap(reply):
    if field(reply, 'is_error', 'isError', False):
        raise ValueError('Mail RPC failed')
    value = field(reply, 'structured_content', 'structuredContent')
    if value is not None:
        return value
    for block in reply.content:
        if getattr(block, 'text', None):
            return json.loads(block.text)
    raise ValueError('Mail RPC has no receipt')


def check_binding(status, who, identity, proxy):
    if (status.get('agent_name') != identity['agent_name'] or status.get('project_key') != identity['project_key']
            or status.get('program') != proxy['env']['AGENTSTACK_PROXY_PROGRAM']
            or status.get('lineage', {}).get('parent_agent') != identity['parent']):
        raise ValueError('Mail binding mismatch')
    if who.get('name') != identity['agent_name'] or who.get('program') != 'hermes' or who.get('retired_at'):
        raise ValueError('Mail remote identity mismatch')


def normalize_reservations(result, paths, owner, now=None):
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    granted = result.get('granted', [])
    if result.get('conflicts') or len(granted) != len(paths) or {r['path_pattern'] for r in granted} != set(paths):
        raise ValueError('Reservation conflict or partial result')
    rows = []
    for row in granted:
        if (type(row.get('id')) is not int or row['id'] <= 0 or row.get('owner', owner) != owner
                or datetime.fromisoformat(row['expires_ts'].replace('Z', '+00:00')).timestamp() <= now + 600):
            raise ValueError('Invalid reservation receipt')
        rows.append({key: row[key] for key in ('id', 'path_pattern', 'expires_ts')} | {'owner': owner})
    return rows


def mail_receipt(value):
    """Accept structured data or Hermes's rendered MCP result; never model prose."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, dict) or value.get('error'):
        return None
    receipt = value.get('structuredContent', value.get('result', value))
    if isinstance(receipt, str):
        try:
            receipt = json.loads(receipt)
        except ValueError:
            return None
    if not isinstance(receipt, dict):
        return None
    deliveries = receipt.get('deliveries')
    if not isinstance(deliveries, list) or len(deliveries) != 1:
        return None
    payload = deliveries[0].get('payload', {})
    message_id = payload.get('id')
    return {'message_id': message_id, 'to': payload.get('to'), 'from': payload.get('from'),
            'verified_sender': receipt.get('verified_sender') is True,
            'ok': receipt.get('verified_sender') is True and type(message_id) is int and message_id > 0}


async def exercise(session, state, action, report):
    config, identity, attempt = [read(state / name) for name in
                               ('run-config.json', 'mail-identity.json', 'attempt.json')]
    proxy = read(state / 'mcp.json')['mcpServers']['orrery-mail']
    listing = await session.list_tools()
    report['tools'] = [tool.name for tool in listing.tools]
    report['schema'] = REQUIRED.issubset(report['tools'])
    if not report['schema']:
        raise ValueError('Mail schema incomplete')
    status = unwrap(await session.call_tool('runtime_status', {}))
    who = unwrap(await session.call_tool('whois', {'name': identity['agent_name']}))
    check_binding(status, who, identity, proxy)
    report['backend'] = True
    if report.get('fixture'):
        raise ValueError('Negative fixture unexpectedly authenticated')
    if attempt['phase'] != 'run' or attempt['agent_name'] != identity['agent_name']:
        raise ValueError('Real action requires the bound run attempt')
    if action == 'reserve':
        task = read(state / 'task.json')
        save(state / 'reservation-intent.json', {'at': stamp(), 'paths': task['allowed_paths']}, exclusive=True)
        result = unwrap(await session.call_tool('reserve_files', {'paths': task['allowed_paths'], 'exclusive': True,
            'ttl_seconds': 1800, 'reason': 'Managed Hermes task'}))
        save(state / 'reservation-result.json', result)
        rows = normalize_reservations(result, task['allowed_paths'], identity['agent_name'])
        save(state / 'reservations.json', {'agent_name': identity['agent_name'],
                                        'attempt_id': attempt['attempt_id'], 'receipts': rows})
    elif action == 'send':
        task = read(state / 'task.json')
        save(state / 'report-intent.json', {'attempt_id': attempt['attempt_id'], 'at': stamp()}, exclusive=True)
        result = await session.call_tool('send_message', {'to': [task['report_to']],
            'subject': 'Managed Hermes proxy preflight', 'body_md': 'Bound proxy authentication and receipt probe.'})
        receipt = mail_receipt(unwrap(result))
        save(state / 'mail-send-receipt.json', receipt)
        if not receipt or not receipt['ok'] or receipt['from'] != identity['agent_name'] or receipt['to'] != [task['report_to']]:
            raise ValueError('Mail receipt not verified')
    elif action == 'release':
        runtime, reservations = read(state / 'runtime-exit.json'), read(state / 'reservations.json')
        if runtime.get('group_absent') is not True or runtime.get('group_check') != 'observed':
            raise ValueError('Owned group absence is unknown')
        if reservations['attempt_id'] != attempt['attempt_id'] or reservations['agent_name'] != identity['agent_name']:
            raise ValueError('Release identity mismatch')
        ids = [row['id'] for row in reservations['receipts']]
        result = unwrap(await session.call_tool('release_reservations', {'file_reservation_ids': ids}))
        confirmed = bool(ids) and result.get('released') == len(ids)
        save(state / 'release-receipt.json', {'attempt_id': attempt['attempt_id'], 'agent_name': identity['agent_name'],
             'reservation_ids': ids, 'released_ids': ids if confirmed else [], 'confirmed': confirmed,
             'source': 'product-bound-proxy', 'checked_at': stamp()})
        if not confirmed:
            raise ValueError('Release not confirmed')
    report['ok'] = True


async def probe(state, action='probe', fixture=''):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    state = Path(state)
    config, identity = read(state / 'run-config.json'), read(state / 'mail-identity.json')
    original = read(state / 'mcp.json')['mcpServers']['orrery-mail']
    expected = str(Path(config['product_root']) / 'integrations/codex_app/plugin/scripts/run-mcp.sh')
    if (original['command'] != expected or original.get('args') != []
            or original['env']['AGENTSTACK_PROXY_AGENT_NAME'] != identity['agent_name']
            or original['env']['AGENTSTACK_PROJECT_KEY'] != identity['project_key']):
        raise ValueError('Generated product proxy config mismatch')
    if fixture not in ('', 'backend-unreachable', 'process-unreachable'):
        raise ValueError('Unknown fixture')
    env = {**original['env'], 'HOME': config['home'], 'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8'}
    command = original['command']
    if fixture == 'backend-unreachable':
        env['AGENTSTACK_MCP_URL'] = 'http://127.0.0.1:1/mcp'
    elif fixture == 'process-unreachable':
        command = '/usr/bin/false'
    report = {'schema': False, 'backend': False, 'ok': False, 'fixture': fixture,
              'agent_name': identity['agent_name'], 'at': stamp()}
    try:
        async with asyncio.timeout(20):
            # Discard untrusted stderr rather than persisting tokens from failures.
            with open('/dev/null', 'w') as errlog:
                params = StdioServerParameters(command=command, args=original['args'], env=env, cwd=config['worktree'])
                async with stdio_client(params, errlog=errlog) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        await exercise(session, state, action, report)
    except (Exception, BaseExceptionGroup) as error:
        report['failure'] = 'MAIL_STEP_FAILED'
        report['failure_type'] = type(error).__name__
    finally:
        save(state / (f'mail-probe-{fixture}.json' if fixture else 'mail-probe.json'), report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('action', choices=['probe', 'reserve', 'send', 'release'], default='probe', nargs='?')
    parser.add_argument('--fixture', default='')
    args = parser.parse_args()
    result = asyncio.run(probe(args.state, args.action, args.fixture))
    print(json.dumps(result))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
