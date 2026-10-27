"""Exercise the documented request with a fake curl, without HTTP or registration."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills/delegate/SKILL.md'
START = '<!-- native-delegate:start -->\n'
END = '<!-- native-delegate:end -->\n'


def native_section():
    return SKILL.read_text().split(START, 1)[1].split(END, 1)[0]


def test_claude_codex_procedure_remains_byte_identical():
    # Protect the reviewed Claude/Codex procedure after the Codex defaults change.
    before, remainder = SKILL.read_bytes().split(START.encode(), 1)
    _, after = remainder.split(END.encode(), 1)
    assert hashlib.sha256(before + after).hexdigest() == (
        '86e9731023ed25d4e5c8d1398ba16b52be1a19be47ea9468bfbe10af862a1ab3')


def test_documented_request_uses_current_public_api_contract():
    body = native_section()
    for value in ('DeepSeek', 'Local', 'Strata', 'dsh', 'Hermes',
                  '/api/spawn-names', '/api/spawn-status?name=', 'AGENT_NAME',
                  'launching', 'ready', 'failed', '0600'):
        assert value in body
    assert SKILL.read_text().index(START) < SKILL.read_text().index('## Naming Rules')
    script = re.search(r"<<'PY'\n(.*?)\nPY", body, re.S)[1]
    tree = ast.parse(script)
    payload = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == 'payload'
                           for target in node.targets))
    fields = {key.value for key in payload.keys}
    assert fields == {'provider', 'harness', 'model', 'parent', 'dir', 'task', 'async'}
    api = (ROOT / 'docs/api.md').read_text()
    for field in fields:
        assert f'`{field}`' in api


@pytest.mark.parametrize('outcome', ['pending', 'refused', 'transport'])
def test_documented_curl_request_and_cleanup(tmp_path, outcome):
    tools = tmp_path / 'bin'
    tools.mkdir()
    # Use the same python3 name as the example, without requiring an ambient tool.
    python = shutil.which('python3')
    assert python
    (tools / 'python3').symlink_to(python)
    curl = tools / 'curl'
    curl.write_text('''#!/usr/bin/env python3
import json, os, pathlib, stat, sys
args = sys.argv[1:]
record = pathlib.Path(os.environ['RECORD'])
if '--data-binary' in args:
    request = pathlib.Path(args[args.index('--data-binary') + 1][1:])
    event = {'kind': 'post', 'payload': json.loads(request.read_text()),
             'mode': stat.S_IMODE(request.stat().st_mode), 'file': str(request), 'args': args}
    with record.open('a') as output:
        output.write(json.dumps(event) + '\\n')
    if os.environ['OUTCOME'] == 'transport':
        sys.exit(7)
    if os.environ['OUTCOME'] == 'refused':
        print(json.dumps({'ok': False, 'error': 'unavailable', 'registration_retained': True}))
    else:
        print(json.dumps({'ok': True, 'pending': True, 'child_name': 'BrightCurie'}))
else:
    with record.open('a') as output:
        output.write(json.dumps({'kind': 'status', 'args': args}) + '\\n')
    print(json.dumps({'ok': True, 'name': 'BrightCurie', 'state': 'ready'}))
''')
    curl.chmod(0o700)
    task = tmp_path / 'task.md'
    content = 'A "quoted" task\n`touch forbidden` $(touch forbidden)\n完了は親へ'
    task.write_text(content)
    requests = tmp_path / 'requests'
    requests.mkdir()
    record = tmp_path / 'events.jsonl'
    env = {**os.environ, 'PATH': f'{tools}:/usr/bin:/bin', 'AGENT_NAME': 'ParentTesla',
           'PARENT_AGENT': 'GrandparentTesla', 'DELEGATE_PROVIDER': 'strata',
           'DELEGATE_HARNESS': 'hermes', 'DELEGATE_MODEL': 'formal-model-id',
           'DELEGATE_DIR': str(tmp_path), 'DELEGATE_TASK_FILE': str(task),
           'AGENTSTACK_PORT': '12345', 'TMPDIR': str(requests),
           'RECORD': str(record), 'OUTCOME': outcome}
    script = re.search(r'```bash\n(.*?)\n```', native_section(), re.S)[1]
    result = subprocess.run(['/bin/bash', '-c', script], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    events = [json.loads(line) for line in record.read_text().splitlines()]
    post = events[0]
    assert post['mode'] == 0o600
    assert post['payload'] == {'provider': 'strata', 'harness': 'hermes',
                               'model': 'formal-model-id', 'parent': 'ParentTesla',
                               'dir': str(tmp_path), 'task': content, 'async': True}
    assert 'http://127.0.0.1:12345/api/spawn' in post['args']
    assert not list(requests.iterdir())
    assert not (tmp_path / 'forbidden').exists()
    if outcome == 'pending':
        assert result.returncode == 0, result.stderr
        assert [event['kind'] for event in events] == ['post', 'status']
        assert 'name=BrightCurie' in events[1]['args']
        assert events[1]['args'][-1] == 'http://127.0.0.1:12345/api/spawn-status'
    else:
        assert result.returncode != 0
        assert len(events) == 1  # No automatic replacement POST or false status lookup.
