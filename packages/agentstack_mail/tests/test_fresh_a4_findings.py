"""Fresh A4 command and published recipient contract regressions."""
import asyncio
import json
import os
from pathlib import Path

import pytest
from fastmcp import Client

from agentstack_mail.fresh_launch import command
from agentstack_mail.fresh_proxy import business_schema
from agentstack_mail.fresh_server import build_fresh_server
from agentstack_mail.schema_contract import validate_schema, validate_schema_definition
from agentstack_mail import fresh_delivery as delivery
from agentstack_mail.storage_origin_contract import B1Error
from test_fresh_delivery import prepared_route, mocked_os
from test_fresh_guard import live
from test_fresh_proxy import proxy

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize('session', [None, '00000000-0000-4000-8000-000000000001'])
@pytest.mark.parametrize('network', [None, '', '1', '0', 'off', 'false', 'no'])
def test_fresh_codex_settings_stay_at_one_subcommand_level(tmp_path, monkeypatch, session, network):
    (tmp_path / 'config.toml').write_text('[mcp_servers.agent_mail]\ncommand="old-mail"\n')
    if network is None:monkeypatch.delenv('AGENTSTACK_CODEX_NETWORK', raising=False)
    else:monkeypatch.setenv('AGENTSTACK_CODEX_NETWORK', network)
    monkeypatch.delenv('AGENTSTACK_VAULT', raising=False)
    args = command(ROOT, tmp_path, '/fixture/codex', 'codex',
                   {'plugin_id': 'agentstack-codex-app@fixture'}, tmp_path, tmp_path, session)
    start = 1
    if session:
        assert args[1:3] == ['resume', session]
        start = 3
    flags = args[start:]
    overrides = [flags[i+1] for i, arg in enumerate(flags[:-1]) if arg == '-c']
    assert 'plugins.agentstack-codex-app@fixture.mcp_servers.agentstack.enabled=false' in overrides
    assert 'mcp_servers.agent_mail.enabled=false' in overrides
    assert 'mcp_servers.orrery-mail.command="/bin/bash"' in overrides
    assert ('sandbox_workspace_write.network_access=true' in overrides) == (network not in {'0', 'off', 'false', 'no'})
    assert flags[flags.index('--ask-for-approval')+1] == 'on-request'
    forwarded = json.loads(next(v.split('=', 1)[1] for v in overrides if v.startswith('mcp_servers.orrery-mail.env_vars=')))
    assert forwarded == ['AGENTSTACK_RUNTIME_MODE', 'AGENTSTACK_FRESH_CLIENT_CONTEXT', 'AGENTSTACK_PYTHON']


@pytest.mark.parametrize('name', ['send_message', 'reply_message'])
def test_backend_and_bound_proxy_publish_recipient_selectors(name):
    async def run():
        async with Client(build_fresh_server(core=object())) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            return tools[name].inputSchema
    backend = asyncio.run(run())
    schema = business_schema(name, backend)['properties']['recipients']
    validate_schema_definition(schema)
    assert set(schema['items']['properties']) == {'target_agent_id', 'target_name', 'expected_target_name', 'kind'}
    for good in ([{'target_agent_id': 1}], [{'target_name': 'Receiver', 'kind': 'cc'}],
                 [{'target_agent_id': 2, 'expected_target_name': 'Receiver', 'kind': 'bcc'}]):
        validate_schema(good, schema)
    for bad in ([], [{}], [{'target_agent_id': 1, 'target_name': 'Receiver'}],
                [{'target_agent_id': True}], [{'target_agent_id': 0}],
                [{'target_name': ''}], [{'target_name': 'Receiver', 'kind': 'other'}],
                [{'target_name': 'Receiver', 'unknown': 'value'}], [None]):
        with pytest.raises(ValueError):validate_schema(bad, schema)


@pytest.mark.parametrize('screen,ready', [
    ('› Ask Codex to do anything\n? for shortcuts', True),
    ('› Ask Codex to do anything\nGPT-6.1-Sol default · /fixture/work\n'
     '? for shortcuts                       ⚠ 3 warnings · f2 to view', True),
    ('› Ask Codex to do anything\nGPT-6.1-Sol default · /fixture/work\n'
     '? for shortcuts\nunknown continuation', False),
    ('❯\u00a0\n────────────────\n  ⏵⏵ auto mode on (shift+tab to cycle)', True),
    ('› Ask Codex to do anything else', False),
    ('❯ 親から COMPLETE が届いたので確認して進めてください', False),
])
def test_fresh_delivery_admission_uses_observed_composer(tmp_path, monkeypatch, screen, ready):
    async def run():
        async with live(tmp_path) as (_, _, client, _):
            run, _ = await prepared_route(client, tmp_path, monkeypatch)
            # The recorded native receipt determines which composer is read.
            provider = 'codex' if screen.startswith('›') else 'claude'
            from agentstack_mail.fresh_launch import layout
            for path in (run/'intent.json', run/'session.json', *list((layout(client)/'sessions').glob('*.json'))):
                value = json.loads(path.read_text());value['provider'] = provider
                path.write_text(json.dumps(value));path.chmod(0o600)
            commands, _, _ = mocked_os(monkeypatch, screen=screen)
            if ready:delivery.target(client, ROOT)
            else:
                with pytest.raises(B1Error, match='FRESH_TMUX_TARGET_NOT_READY'):delivery.target(client, ROOT)
            assert commands and all(row[0][0] == 'capture-pane' for row in commands)
    asyncio.run(run())


def test_old_localized_birth_stays_refused_instead_of_rebinding(tmp_path, monkeypatch):
    async def run():
        async with live(tmp_path) as (_, _, client, _):
            run, row = await prepared_route(client, tmp_path, monkeypatch)
            old = '水 10/21 22:30:25 2026'
            current = 'Wed Oct 21 22:30:25 2026'
            row['pane_process_start'] = old
            row['runner']['process_start'] = row['provider']['process_start'] = old
            (run/'tmux.json').write_text(json.dumps(row));(run/'tmux.json').chmod(0o600)
            commands, _, processes = mocked_os(monkeypatch)
            processes['process_snapshot'] = lambda: {100: (1, os.getuid(), current),
                101: (100, os.getuid(), current), 102: (101, os.getuid(), current)}
            with pytest.raises(B1Error, match='FRESH_TMUX_TARGET_CHANGED'):delivery.target(client, ROOT)
            assert commands == []
    asyncio.run(run())


@pytest.mark.parametrize('draft', [
    'GPT-6.1-Sol default · please review this transcript',
    '⚠ 3 warnings · f2 to view',
])
def test_multiline_footer_shaped_draft_is_not_delivered(tmp_path, monkeypatch, draft):
    # Adapted from RockyTuring's review regression: real pending delivery,
    # isolated Mail fixture and mocked tmux, never a live pane.
    from agentstack_mail.fresh_launch import layout
    from test_fresh_mail import send_args
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            run, _ = await prepared_route(client, tmp_path, monkeypatch)
            for path in (run/'intent.json', run/'session.json', *list((layout(client)/'sessions').glob('*.json'))):
                value = json.loads(path.read_text());value['provider'] = 'codex'
                path.write_text(json.dumps(value));path.chmod(0o600)
            screen = '›\n  ' + draft + '\n\n  GPT-6.1-Sol default · /fixture/work\n? for shortcuts'
            commands, _, _ = mocked_os(monkeypatch, screen=screen)
            await runtime.core.write_message('send', **send_args(runtime, people,
                auth=people[1]['auth'], recipients=[{'target_name': 'Alpha'}]))
            with pytest.raises(B1Error, match='FRESH_TMUX_TARGET_NOT_READY'):
                await delivery.deliver(client, ROOT)
            assert all(args[0] not in ('paste-buffer', 'send-keys') for args, _ in commands)
    asyncio.run(run())


def test_bound_stdio_catalogue_preserves_detailed_recipient_schema_and_send(tmp_path):
    async def run():
        async with live(tmp_path) as (_, people, client, _):
            async with proxy(client, tmp_path) as peer:
                definitions = {tool['name']: tool for tool in (await peer.rpc('tools/list'))['result']['tools']}
                for name in ('send_message', 'reply_message'):
                    schema = definitions[name]['inputSchema']['properties']['recipients']
                    assert schema['items']['properties']['target_agent_id']['minimum'] == 1
                    validate_schema([{'target_agent_id': people[1]['agent_id']}], schema)
                result = await peer.value('send_message', {'recipients': [{'target_agent_id': people[1]['agent_id'],
                    'expected_target_name': 'Beta', 'kind': 'to'}], 'subject': 'A4', 'body_md': 'bounded fixture'})
                assert result['recipients'][0]['agent_id'] == people[1]['agent_id']
    asyncio.run(run())
