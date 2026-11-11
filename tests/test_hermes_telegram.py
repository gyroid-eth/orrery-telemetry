"""Existing Telegram plugin, synthetic transport and credential readers only."""
import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fixture_telegram', ROOT / 'integrations/hermes/telegram/__init__.py',
    submodule_search_locations=[str(ROOT / 'integrations/hermes/telegram')])
plugin = importlib.util.module_from_spec(spec);sys.modules[spec.name] = plugin;spec.loader.exec_module(plugin)


@pytest.mark.parametrize('store', ['keychain', 'secret-service'])
def test_reference_reader_stays_in_host_and_dm_reply_keeps_allowlist(tmp_path, store, monkeypatch):
    ref = (dict(store='keychain', service='fixture.telegram', account='fixture') if store == 'keychain'
           else dict(store='secret-service', attributes={'application':'fixture-telegram'}))
    value = dict(token_ref=ref, allow_from=[123])
    calls = []
    original_reader = plugin.settings.reader
    def fake_run(argv, **kwargs):
        calls.append(argv)
        assert 'fake-private-token' not in argv
        return subprocess.CompletedProcess(argv, 0, 'fake-private-token\n' if kwargs.get('text') else b'fake-private-token\n')
    if store == 'keychain':
        monkeypatch.setattr(plugin.settings.sys, 'platform', 'darwin')
    else:
        def reader():
            api = original_reader()
            api['read_ref'].__globals__['validate_environment'] = lambda: None
            api['read_ref'].__globals__['subprocess'].run = fake_run
            return api
        monkeypatch.setattr(plugin.settings, 'reader', reader)
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
    monkeypatch.delenv('HERMES_TELEGRAM_BOT_TOKEN', raising=False)
    with patch.object(plugin.settings.subprocess, 'run', fake_run):
        token = plugin.settings.read_token(ref)
    assert token == 'fake-private-token' and len(calls) == 1
    # Actual model subprocess inherits references, never the in-process value.
    # Restore subprocess.run after the Secret Service module uses it too.
    assert not subprocess.run(['/bin/bash','-c','printenv TELEGRAM_BOT_TOKEN HERMES_TELEGRAM_BOT_TOKEN'], capture_output=True).stdout
    with patch.object(plugin, '_configuration', return_value=value), patch.object(plugin.settings, 'read_token', return_value=token):
        channel = plugin.TelegramChannel(SimpleNamespace(_manager=SimpleNamespace(home_path=tmp_path)))
    injected = []
    channel.ctx.inject_message = lambda text, **kwargs: injected.append((text, kwargs)) or True
    channel._on_session(session_id='fixture-session')
    def update(user, kind='private'):
        return dict(message=dict(chat=dict(type=kind,id=123), **{'from':dict(id=user)}, message_id=7, text='Hello'))
    channel._handle_update(update(999));channel._handle_update(update(123, 'group'))
    assert not injected
    channel._handle_update(update(123))
    assert len(injected) == 1 and injected[0][1]['session_key'] == 'fixture-session'
    assert 'chat_id=123 message_id=7' in injected[0][0]
    posted = []
    async def post(method, payload):
        posted.append((method,payload));return {'ok':True,'result':{'message_id':8}}
    channel._api = post
    assert asyncio.run(channel._send_async(123, 'Reply', 7)) == [8]
    assert posted[0][0] == 'sendMessage' and posted[0][1]['reply_parameters']['message_id'] == 7
    assert json.loads(channel._reply_tool(dict(chat_id=999,text='No')))['ok'] is False
    owner = plugin.managed_guard.PollOwner(tmp_path)
    duplicate = plugin.managed_guard.PollOwner(tmp_path)
    assert owner.acquire()
    try:assert duplicate.acquire() is False
    finally:owner.release()
    assert duplicate.acquire();duplicate.release()
