"""TEST-only fresh S2c lifetimes at one literal path/port, never live Mail.

Previous-process exit and product health bindings, not a fixture tag, establish
which server answered. Candidate data is rebuilt by existing preparation APIs.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid

from .caller_payload import ROOT
from .support import registered_address


def health_binding(value):
    keys = ('server_instance_id', 'candidate_generation', 'authority_epoch')
    assert all(isinstance(value.get(key), str) and value[key] for key in keys), \
        'fixture server health has no product binding'
    return {key:value[key] for key in keys}


def wait_fixture_health(process, management, probe, *, timeout=25, expected_binding=None):
    """Bound HTTP-startup retries, but never admit another valid generation."""
    deadline = time.monotonic()+timeout
    last = 'HTTP health has not answered'
    while True:
        assert process.poll() is None, 'isolated Mail server stopped during preparation'
        remaining = deadline-time.monotonic()
        assert remaining > 0, 'isolated Mail server health timeout: '+last
        if management.exists():
            try:
                value = asyncio.run(asyncio.wait_for(probe(), timeout=min(1, remaining)))
            except Exception as error:
                last = type(error).__name__
            else:
                if (value.get('status'), value.get('runtime_profile')) == ('ok', 's2c-v1'):
                    if expected_binding is not None:
                        assert health_binding(value) == expected_binding, \
                            'fixture connected to a different server generation'
                    assert process.poll() is None, 'fixture server exited after HTTP health'
                    return value
                last = 'unexpected runtime profile'
        time.sleep(min(.05, remaining))


@dataclass
class LiveServer:
    config: Path
    url: str
    process: subprocess.Popen
    binding: dict


@contextmanager
def isolated_server(workspace, *, port):
    """Prepare and run one real owned server; no database copying or editing."""
    from agentstack_mail.global_prepare_s2c import prepare
    from agentstack_mail.namespace_plan import FenceEvidence
    from fastmcp import Client
    spec = importlib.util.spec_from_file_location('r3_fixture_namespace',
        ROOT/'packages/agentstack_mail/tests/fixtures/namespace_state.py')
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    isolation = workspace.tmp/'isolation'
    isolation.mkdir(mode=0o700)
    (isolation/'home').mkdir(mode=0o700)
    (isolation/'tmux').mkdir(mode=0o700)
    source, choices = legacy.build(isolation/'source')
    def fence(generation, roster):
        return FenceEvidence(generation, roster, {name:'stopped' for name in roster},
                             True, True, True, 0)
    prepare(source, isolation, 'one', choices, request_id=str(uuid.uuid4()), fence=fence)
    for path in (isolation/'agentstack', isolation/'agentstack/clients'):
        path.mkdir(mode=0o700, exist_ok=True)
        path.chmod(0o700)
    config = isolation/'s2c-candidates/one/candidate/server-config.json'
    saved = json.loads(config.read_text())
    expected = {'server_instance_id':saved['mail_instance_id'],
                'candidate_generation':saved['candidate_generation'],
                'authority_epoch':saved['authority_epoch']}
    address = ('127.0.0.1', port)
    url = f'http://127.0.0.1:{port}/mcp'
    env = {key:value for key,value in os.environ.items()
           if not key.startswith(('AGENTSTACK_', 'AGS_'))}
    env.update(HOME=str(isolation/'home'), TMUX_TMPDIR=str(isolation/'tmux'),
        PYTHONPATH=str(ROOT/'packages/agentstack_mail/src'),
        AGENTSTACK_MAIL_ENV_FILE=str(isolation/'absent-env'),
        AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE='passthrough')
    with registered_address(address), (isolation/'server.log').open('w') as log:
        process = subprocess.Popen([sys.executable, '-m', 'agentstack_mail.cli',
            '--port', str(port), '--global-config', str(config)],
            cwd=isolation, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            async def health():
                async with Client(url) as client:
                    response = await client.call_tool('health_check', {})
                    return json.loads(next(part.text for part in response.content if hasattr(part, 'text')))
            observed = wait_fixture_health(process, Path(saved['management_socket']), health,
                                           expected_binding=expected)
            yield LiveServer(config, url, process, health_binding(observed))
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


class FreshServerLifetime:
    """One actual server per entry; keep only its process/binding evidence."""
    def __init__(self, workspace):
        self.workspace = workspace
        self.root = workspace.tmp
        assert self.root.resolve() == self.root and self.root.name.startswith('r3-caller-')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            self.port = listener.getsockname()[1]
        assert self.port not in (8765, 8770), 'fixture selected a live-service port'
        self.previous = None
        self.previous_binding = None
        self.active = False
        self.history = []
        # prepare_caller_invocation creates this chain before an entry runs.
        for relative in ('isolation', 'isolation/agentstack', 'isolation/agentstack/clients'):
            path = self.root/relative
            path.mkdir(mode=0o700)

    def reset_isolation(self):
        target = self.root/'isolation'
        assert target.parent == self.workspace.tmp and target.name == 'isolation'
        assert self.root.name.startswith('r3-caller-') and self.root.resolve() == self.root
        if target.exists() or target.is_symlink():
            info = target.lstat()
            assert (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                    and stat.S_IMODE(info.st_mode) == 0o700 and target.resolve() == target), \
                'unsafe fixture isolation root'
            shutil.rmtree(target)

    @contextmanager
    def entry(self):
        assert not self.active, 'previous fixture server is still active'
        assert self.previous is None or self.previous.poll() is not None, \
            'previous fixture server is still alive'
        self.active = True
        try:
            self.reset_isolation()
            with isolated_server(self.workspace, port=self.port) as live:
                self.previous = live.process
                if self.previous_binding is not None:
                    assert live.binding['authority_epoch'] != self.previous_binding['authority_epoch'], \
                        'fixture reused the previous server authority'
                self.previous_binding = dict(live.binding)
                record = {'pid':live.process.pid, 'binding':dict(live.binding), 'returncode':None}
                self.history.append(record)
                yield live
        finally:
            self.active = False
            if self.previous is not None:
                code = self.previous.poll()
                assert code is not None, 'previous fixture server did not stop'
                if self.history and self.history[-1]['pid'] == self.previous.pid:
                    self.history[-1]['returncode'] = code
