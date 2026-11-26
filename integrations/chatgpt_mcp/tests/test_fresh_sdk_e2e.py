"""The installed MCP SDK discovers and executes the fresh workflow over HTTP."""
import asyncio

from fastmcp import Client

from orrery_chatgpt_mcp.server import read_secret
from test_fresh_message_e2e import live, stored


def test_sdk_initialize_discover_preview_commit_status(live):
    async def run():
        url=f'http://127.0.0.1:{live.port}/mcp/{read_secret(live.cfg)}'
        async with Client(url) as client:
            tools={tool.name:tool for tool in await client.list_tools()}
            assert set(tools)=={'orrery_status','orrery_list_agents','orrery_get_agent',
                               'orrery_message_preview','orrery_message_commit','orrery_message_status'}
            assert tools['orrery_message_commit'].annotations.readOnlyHint is False
            annotations = tools['orrery_message_status'].annotations
            assert annotations.readOnlyHint is True
            assert annotations.destructiveHint is False
            assert annotations.idempotentHint is True
            assert annotations.openWorldHint is False
            preview=(await client.call_tool('orrery_message_preview',live.args)).data
            assert preview['status']=='previewed' and preview['message']['body_md']==live.args['body_md']
            args={key:preview[key] for key in ('preview_id','content_digest')}
            args['idempotency_key']=live.args['idempotency_key']
            accepted=(await client.call_tool('orrery_message_commit',args)).data
            before = live.gateway.messages.state.path.read_bytes()
            checked=(await client.call_tool('orrery_message_status',{'idempotency_key':live.args['idempotency_key']})).data
            assert accepted==checked and accepted['status']=='accepted'
            assert live.gateway.messages.state.path.read_bytes() == before
            assert accepted['execution_status']=='unobserved'
            assert (await client.call_tool('orrery_message_commit',args)).data==accepted
    asyncio.run(run())
    assert len(stored(live)[0])==1 and stored(live)[0][0][-1]==live.args['body_md']
