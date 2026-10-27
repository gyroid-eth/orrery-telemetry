"""Private resident host lifecycle and fresh authority; no live services."""
import asyncio
import json
import os
from pathlib import Path
import runpy

import pytest

from dashboard.fresh_backend import Backend
from dashboard.fresh_host import Host
from test_fresh_guard import live
from test_fresh_targets import ROOT, setup, replace
from agentstack_mail.fresh_runtime import _read_private


def test_private_host_rechecks_current_and_context_and_closes(tmp_path, monkeypatch):
    async def scenario():
        async with live(tmp_path) as (_, people, client, _):
            run, _, _, _, _ = await setup(client, tmp_path, monkeypatch)
            host = Host(Backend(client.path, ROOT))
            wire = runpy.run_path(str(ROOT / 'bin/lib/runtime_tmux.py'))
            await asyncio.to_thread(host.start)
            try:
                async def call(value, identity=None):
                    return await asyncio.to_thread(wire['fresh_host_call'], host.path, value, expected_socket=identity)
                sock, hello = await call({'operation': 'hello'})
                pin = hello['pin']
                assert hello['result'] == {'ok': True}
                assert pin['mail_instance_id'] == client.context['mail_instance_id']
                _, catalog = await call({'operation': 'catalog', 'pin': pin}, sock)
                assert catalog['result']['agents'][0]['mail_agent_id'] == people[0]['agent_id']
                query = {'operation': 'inspect', 'session': 'Alpha', 'pin': pin}
                _, initial = await call(query, sock)
                revision = initial['result']['revision']
                member={'operation':'membership','pin':pin,'expected':revision,
                    **{k:initial['result'][k] for k in ('session','mail_instance_id','mail_agent_id')}}
                _, accepted=await call(member,sock)
                assert accepted['result']['revision']==revision
                intent_path=run/'intent.json';intent=_read_private(intent_path)
                replace(intent_path,dict(intent,cwd=str(tmp_path/'different')))
                _, changed_intent=await call(member,sock)
                assert 'error' in changed_intent
                replace(intent_path,intent)
                _, initial=await call(query,sock)
                assert initial['result']['revision']==revision
                _, again = await call(dict(query, expected=revision), sock)
                assert again['result']['revision'] == revision
                current = run.parent.parent / 'current.json'
                saved = _read_private(current)
                replace(current, {'launch_id': '00000000-0000-4000-8000-000000000001'})
                _, rejected=await call(member,sock)
                assert 'error' in rejected
                _, changed = await call(dict(query, expected=revision), sock)
                assert 'error' in changed
                replace(current, saved)
                context = _read_private(client.path)
                replace(client.path, dict(context, credential_generation=context['credential_generation'] + 1))
                _, revoked = await call({'operation': 'catalog', 'pin': pin}, sock)
                assert revoked['error'] == 'FRESH_CLIENT_CONTEXT_CHANGED'
                replace(client.path, context)
                _, invalid = await call({'operation': 'inspect', 'pin': pin, 'path': '/other'}, sock)
                assert invalid['error'] == 'FRESH_TARGET_QUERY_INVALID'
                assert 'registration_token' not in json.dumps([hello, catalog, initial])
                duplicate = Host(Backend(client.path, ROOT))
                with pytest.raises((OSError, RuntimeError)):
                    await asyncio.to_thread(duplicate.start)
            finally:
                await asyncio.to_thread(host.close)
            assert not host.path.exists()
            replacement = Host(Backend(client.path, ROOT))
            await asyncio.to_thread(replacement.start)
            try:
                _, next_hello = await asyncio.to_thread(wire['fresh_host_call'], replacement.path, {'operation': 'hello'})
                assert next_hello['pin']['epoch'] != pin['epoch']
                with pytest.raises(wire['TargetError'], match='FRESH_HOST_CHANGED'):
                    await asyncio.to_thread(wire['fresh_host_call'], replacement.path, {'operation': 'hello'}, expected_socket=sock)
            finally:
                await asyncio.to_thread(replacement.close)
    asyncio.run(scenario())
