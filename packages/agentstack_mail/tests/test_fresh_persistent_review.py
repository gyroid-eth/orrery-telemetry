"""A restarting persistent run cannot hand off or consume pending Mail."""
import asyncio
from io import StringIO
import json
from pathlib import Path
from uuid import uuid4

import pytest

from agentstack_mail import fresh_delivery as delivery, fresh_notifications as notices
from agentstack_mail.fresh_launch import record_session
from agentstack_mail.storage_origin_contract import B1Error
from test_fresh_delivery import prepared_route, mocked_os, ROOT
from test_fresh_guard import live
from test_fresh_mail import send_args
from test_fresh_notifications import rows


def test_restart_required_keeps_mail_pending_for_watcher_and_old_post_hook(tmp_path, monkeypatch):
    async def check():
        async with live(tmp_path) as (runtime, people, client, _):
            run, _ = await prepared_route(client, tmp_path, monkeypatch)
            intent = json.loads((run / 'intent.json').read_text());intent['persistent'] = True
            (run / 'intent.json').write_text(json.dumps(intent))
            saved = json.loads((run / 'session.json').read_text())
            new_id = str(uuid4())
            transcript = Path(saved['transcript_path']).with_name(new_id + '.jsonl')
            transcript.write_text('synthetic second transcript')
            with pytest.raises(B1Error, match='FRESH_BOT_RESTART_REQUIRED'):
                await record_session(client, dict(hook_event_name='SessionStart', source='clear',
                    session_id=new_id, transcript_path=str(transcript)), defer_transcript=True)
            commands, _, _ = mocked_os(monkeypatch)
            await runtime.core.write_message('send', **send_args(runtime, people,
                auth=people[1]['auth'], recipients=[{'target_name':'Alpha'}]))
            output = StringIO()
            for consumer in ('watcher', 'old-post-hook'):
                try:
                    if consumer == 'watcher':await delivery.deliver(client, ROOT)
                    else:await notices.hook(client, dict(hook_event_name='PostToolUse',
                        session_id=saved['native_session_id'], transcript_path=saved['transcript_path']), ROOT, output=output)
                except B1Error as error:assert error.code == 'FRESH_BOT_RESTART_REQUIRED'
                facts, _ = await rows(runtime)
                assert facts[0]['completed_ts'] is None, consumer
                assert not output.getvalue()
                assert not any(args[0] in {'paste-buffer', 'send-keys'} for args, _ in commands)
    asyncio.run(check())
