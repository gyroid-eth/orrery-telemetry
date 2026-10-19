"""TEST server-lifetime controls; not native or all-caller acceptance."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from .caller_server import FreshServerLifetime, health_binding, wait_fixture_health


@pytest.fixture
def lifetime():
    base = '/private/tmp' if Path('/private/tmp').is_dir() else None
    with tempfile.TemporaryDirectory(prefix='r3-caller-', dir=base) as raw:
        yield FreshServerLifetime(SimpleNamespace(tmp=Path(raw).resolve(strict=True)))


def read_health(url):
    from fastmcp import Client
    async def read():
        async with Client(url) as client:
            result = await client.call_tool('health_check', {})
            return json.loads(next(part.text for part in result.content if hasattr(part, 'text')))
    return asyncio.run(read())


def test_consecutive_real_servers_keep_paths_port_and_assigned_id_but_not_authority(lifetime):
    from .caller_payload import ROOT
    observations = []
    for _ in range(2):
        with lifetime.entry() as live:
            actual = health_binding(read_health(live.url))
            assert actual == live.binding
            assert live.process.poll() is None
            # Like the caller adapter, use the real management client in its
            # subprocess. Do not weaken the host's TCP-only network guard.
            env = {**os.environ, 'AGENTSTACK_MCP_URL':live.url,
                   'PYTHONPATH':str(ROOT/'bin/lib')}
            completed = subprocess.run([sys.executable, '-c',
                'import json,sys; from runtime_operator import RuntimeOperator; '
                'print(json.dumps(RuntimeOperator(sys.argv[1]).create("fixed", '
                'agent_name="FixtureStable", program="daemon", model="deterministic")))',
                str(live.config)], env=env, capture_output=True, text=True, timeout=20)
            assert completed.returncode == 0, completed.stderr
            result = json.loads(completed.stdout)
            assert result['ok'] and type(result['agent_id']) is int
            observations.append((live.config, live.url, result['agent_id'], actual))
        assert live.process.poll() is not None
        assert lifetime.history[-1]['returncode'] is not None
    first, second = observations
    assert first[:3] == second[:3], 'literal path/port or actual assigned agent ID changed'
    assert first[3]['authority_epoch'] != second[3]['authority_epoch']
    assert len(lifetime.history) == 2


def test_previous_live_server_is_rejected_even_if_fixture_active_marker_is_lost(lifetime):
    with lifetime.entry() as live:
        before = live.config.read_bytes()
        lifetime.active = False  # prove the actual Popen guard, not just a flag
        try:
            with pytest.raises(AssertionError, match='previous fixture server is still alive'):
                with lifetime.entry():
                    pytest.fail('live predecessor was accepted')
        finally:
            lifetime.active = True
        assert live.process.poll() is None
        assert live.config.read_bytes() == before
        assert health_binding(read_health(live.url)) == live.binding


@pytest.mark.parametrize('drift', [None, 'server_instance_id', 'candidate_generation', 'authority_epoch'])
def test_health_cannot_accept_stale_product_binding(workspace, drift):
    management = workspace.tmp/'fixture-control.sock'
    management.touch()
    expected = {'server_instance_id':'fixture-instance',
                'candidate_generation':'current-generation', 'authority_epoch':'current-authority'}
    value = {'status':'ok', 'runtime_profile':'s2c-v1', **expected}
    if drift:
        value[drift] = 'old-server-value'
    async def probe():return value
    process = SimpleNamespace(poll=lambda:None)
    if drift:
        with pytest.raises(AssertionError, match='fixture connected to a different server generation'):
            wait_fixture_health(process, management, probe, expected_binding=expected)
    else:
        assert wait_fixture_health(process, management, probe, expected_binding=expected) == value
